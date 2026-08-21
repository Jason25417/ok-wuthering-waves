# -*- coding: utf-8 -*-
"""main_chain.py —— 给外层调度器用的入口，带真实退出码。

## 为什么需要它

ok-ww 的 CLI 恒返回 0：任务抛异常时 TaskExecutor 的兜底 except 只记日志 + 发通知，
不改变返回值；ok/cli.py 的 run_task_command 也是无条件 return 0。于是外层调度器
拿到 exit 0 时无法区分「日常真做完了」和「第一步就崩了」，只能 grep 日志猜。

## 它怎么工作

框架已把成功与失败分发到公开信号上，只是无头路径没有订阅者：
  · communicate.task_done —— 只在 task.run() 正常返回后 emit（TaskExecutor.py:573）。
  · communicate.notification —— 异常分支 emit(错误信息, task.name, True, True, None, params)。
  · communicate.screenshot —— 失败帧走这条（TaskExecutor.py:618，emit(frame, name, True, None)），
    **不是** notification 的第 6 参（那里只有 None 或热键异常的 {'key':...}，
    2026-08-21 审查实证：把取证挂在 notification 上是死代码）。

本脚本订阅这三个信号，跑完按结果决定退出码，失败时关游戏并写结构化结果。

## 关键前提：必须走 headless 路径（2026-08-21 审查抓到的 blocker）

config.py 的 use_gui=True 时 OK.start() 走 GUI 分支：show_main_window() + app.exec()，
而 App.exec 末尾是 sys.exit(app.exec())——**start() 永不正常返回**，本脚本的失败
分支全是死代码：任务失败后 QApplication 不退出，进程挂到外层 110 分钟止损，
游戏满负荷渲染整晚（正是要消灭的历史故障形态）。
所以这里在构造 OK 之前强制 config['use_gui'] = False；外层再传 -h 双保险。
headless 路径（run_task → wait_task）在任务失败 disable 后 start() 会正常返回。

## 失败判定的过滤（2026-08-21 审查抓到的假失败源）

error=True 的通知不全是任务失败——生命周期通知也用它：
  · 'Game Exited'（成功路径 stop_hwnd 杀游戏后，hwnd 轮询线程必发，第 2 参=None）
  · 'Paused because game window is minimized...'（第 2 参=None）
  · GPU Driver Warning（第 2 参非 None，但第 5 参='start'）
真正的任务失败：第 2 参=task.name（恒非 None）且第 5 参=None（TaskExecutor.py:615）。
按这个组合过滤，否则每次干净成功都会因 'Game Exited' 被误判成失败。

## 用法

与原 main.py 参数一致：pythonw.exe main_chain.py -t 1 -e [-h]

退出码：0=至少一个任务干净跑完且无失败 / 1=有任务失败 / 2=一个都没跑起来 / 3=启动即崩
"""
import sys

_done = []
_failed = []


def _on_task_done(task):
    name = getattr(task, "name", None) or str(task)
    _done.append(name)
    print(f"[main_chain] 任务干净跑完: {name}", flush=True)


def _on_notification(title, message, error, tray, tray_key, params):
    """实参顺序按 emit 处而不是形参名（两者是反的，实测 TaskExecutor.py）：
      :615 异常分支  emit(错误信息, 任务名, True, True, None, params)
      :592 停止通知  emit('Stopped', 任务名, False, ...)
    第 1 参是"说什么"，第 2 参是"谁"。

    过滤规则见模块 docstring：任务失败 = error 且 第2参非None 且 第5参为None。
    """
    if not error:
        return
    if message is None:
        # 生命周期通知（Game Exited / 窗口最小化暂停），不是任务失败
        print(f"[main_chain] 生命周期通知(不计失败): {title}", flush=True)
        return
    if tray_key is not None:
        # 启动检查类警告（GPU Driver Warning 等，tray_key='start'），不是任务失败
        print(f"[main_chain] 启动警告(不计失败): {message}: {str(title)[:80]}", flush=True)
        return
    _failed.append((message, str(title)))
    print(f"[main_chain] 失败信号: {message}: {title}", file=sys.stderr, flush=True)


def _on_screenshot(frame, name, error, frame_box):
    """失败帧另存一份到 logs/failure_frames/，供事后定位卡死画面。

    框架的 Screenshot worker 本来就会把这条信号的帧存进 screenshots/（7 天清理），
    这里只是给失败帧留一份更持久、路径固定的拷贝，晨链好引用。
    signature: communicate.screenshot = Signal(object, str, bool, object)。
    """
    if not error or frame is None:
        return
    try:
        import os
        import time as _t
        out = os.path.join(os.getcwd(), "logs", "failure_frames")
        os.makedirs(out, exist_ok=True)
        safe = "".join(c for c in str(name) if c.isalnum() or c in "-_")[:40] or "task"
        path = os.path.join(out, f"{_t.strftime('%Y%m%d-%H%M%S')}_{safe}.png")
        import cv2
        cv2.imwrite(path, frame)
        print(f"[main_chain] 失败画面已存: {path}", flush=True)
    except Exception as e:
        print(f"[main_chain] 存失败画面出错（忽略）: {e}", file=sys.stderr, flush=True)


def _close_game_after_failure():
    """任务失败后把游戏关掉。

    框架的「Exit After Task」只挂在成功分支（TaskExecutor.py:574 的 if 在
    task.run() 之后的正常路径里），异常分支不碰游戏进程。无人值守时任务一失败，
    鸣潮就留在前台满负荷渲染到人起床——8 天实测踩了 3 天（上游 #1589 同诉求）。

    放在 OK.start() 返回后调用：回调触发时任务线程还在收尾（通知/截图/disable），
    当场杀会打断清理；start() 返回后该记的都记完了。
    """
    try:
        from ok import og
        dm = getattr(og, "device_manager", None)
        if dm is None:
            print("[main_chain] 没拿到 device_manager，跳过关游戏", file=sys.stderr, flush=True)
            return
        dm.stop_hwnd()
        print("[main_chain] 任务失败，已关闭游戏", flush=True)
    except Exception as e:
        print(f"[main_chain] 关游戏失败（不影响退出码）: {e}", file=sys.stderr, flush=True)


def _verdict():
    """按收集到的信号定退出码。"""
    if _failed:
        return 1
    if not _done:
        return 2
    return 0


def main():
    try:
        from config import config
        from ok import OK
        from ok.gui.Communicate import communicate
    except Exception as e:
        print(f"[main_chain] 导入失败: {e}", file=sys.stderr, flush=True)
        return 3

    # blocker 修复：强制无头。GUI 分支的 app.exec() 永不正常返回，
    # 失败时进程挂死、以下所有收尾逻辑都执行不到。
    config['use_gui'] = False

    communicate.task_done.connect(_on_task_done)
    communicate.notification.connect(_on_notification)
    communicate.screenshot.connect(_on_screenshot)

    try:
        ok = OK(config)
        ok.start()
    except SystemExit as e:
        # 理论上强制无头后不该再走到这（GUI 的 sys.exit 路径），保险起见保留：
        # 即使它退 0，也要过我们自己的判定——零任务完成不算成功。
        code = e.code if isinstance(e.code, int) else 0
        rc = _verdict()
        return rc if code == 0 else code
    except Exception as e:
        print(f"[main_chain] 运行期异常: {e}", file=sys.stderr, flush=True)
        return 3

    rc = _verdict()
    if rc != 0:
        if rc == 2:
            print("[main_chain] 没有任何任务跑完，也没有失败信号", file=sys.stderr, flush=True)
        _close_game_after_failure()
    return rc


def _write_result(rc):
    """把本次运行的结构化结果原子写成 logs/chain_result.json，给外层调度器读。

    日志是给人看的、措辞随版本变，grep 判据脆且信息量低。这份 JSON 是机器对机器
    的契约：外层读它时要核对 finished_at 的新鲜度（进程硬死时本文件是上次残留）。
    """
    try:
        import json
        import os
        import time as _t
        out = {
            "schema": 1,
            "finished_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_code": rc,
            "tasks_done": list(_done),
            "failed": [{"task": w, "error": e} for w, e in _failed],
        }
        path = os.path.join(os.getcwd(), "logs", "chain_result.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        print(f"[main_chain] 结果已写入 {path}", flush=True)
    except Exception as e:
        print(f"[main_chain] 写结果文件失败（不影响退出码）: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    rc = main()
    print(f"[main_chain] 完成 {len(_done)} 个 / 失败 {len(_failed)} 个 → 退出码 {rc}", flush=True)
    _write_result(rc)
    sys.exit(rc)
