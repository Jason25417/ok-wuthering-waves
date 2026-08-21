# -*- coding: utf-8 -*-
"""main_chain.py —— 给外层调度器用的入口，带真实退出码。

## 为什么需要它

ok-ww 的 CLI 恒返回 0：任务抛异常时 TaskExecutor 的兜底 except 只是记日志 + 发通知，
不改变返回值（ok/task/TaskExecutor.py 的通用 except 分支）；ok/cli.py 的 run_task_command
也是无条件 return 0。于是外层调度器（这里是晨链）拿到 exit 0 时无法区分
「日常真做完了」和「第一步就崩了」，只能去 grep 日志猜——2026-07-22/23 那次
「退出码 0 但日常没做」就是这么漏过去的。

## 它怎么工作

框架其实已经把成功与失败分发到两个公开信号上，只是无头路径一个订阅者都没有：

  · communicate.task_done —— 只在 task.run() 正常返回后才 emit
    （TaskExecutor.py:573，紧跟在 task.run() / task.disable() 之后）。
    run() 抛异常会被上层 except 接住，走不到这一行。所以它是干净跑完的证据。
  · communicate.notification(..., error=True) —— 异常分支发的，带错误信息与截图。

本脚本在 OK.start() 之前订阅这两个信号，跑完按收集到的结果决定退出码。
**不改框架、不改 src、不改 main.py**——那些文件都在 deploy.txt 的分发范围内，
改了会被下次整树更新抹掉（2026-08-01 的两个补丁就是这么没的）。

## 用法

与原 main.py 完全一致，参数照传：
    pythonw.exe main_chain.py -t 1 -e

退出码：
    0  至少有一个任务干净跑完，且没有任何失败信号
    1  收到过失败信号（哪个任务、什么错，见 stderr 与日志）
    2  一个任务都没跑起来（配置错、游戏没开、窗口没找到等）
    3  启动阶段就抛异常
"""
import sys

# 收集两个信号的结果。用普通 list 而不是 Queue：PySide6 的信号回调在
# 跨线程时走事件循环投递，同线程时直连，两种情况下这里都只是 append，
# 不需要额外同步（GIL 保证 list.append 原子）。
_done = []
_failed = []


def _on_task_done(task):
    name = getattr(task, "name", None) or str(task)
    _done.append(name)
    print(f"[main_chain] 任务干净跑完: {name}", flush=True)


def _on_notification(title, message, error, tray, task_name, images):
    """signature 见 ok/gui/Communicate.py:
       notification = Signal(str, str, bool, bool, str, object)

    实参顺序要看 emit 处而不是形参名 —— 两者是反的（实测 TaskExecutor.py）:
      :615 异常分支  emit(错误信息, 任务名, True,  True, None, params)
      :592 停止通知  emit('Stopped', 任务名, False, ...)
    也就是说第 1 个位置放的是"说什么"，第 2 个位置才是"谁"，而形参 task_name
    在异常分支里是 None。所以取任务名要用 message，取错误内容要用 title。
    """
    if not error:  # info 级通知（如"已停止"）不计入失败
        return
    who = message or task_name or "?"
    what = title or "(无错误信息)"
    _failed.append((who, what))
    print(f"[main_chain] 失败信号: {who}: {what}", file=sys.stderr, flush=True)
    _save_failure_frame(who, what, images)


def _save_failure_frame(who, what, images):
    """把失败瞬间的画面单独存一份，供事后定位。

    框架异常分支本来就会把截图塞进 notification 的第 6 个参数
    （TaskExecutor.py:615 的 params），但无头模式下没人接，画面就丢了。
    存到晨链能看到的地方，下次复现时不用等再翻车一遍。

    这件事有具体的待办在等它：src/task/BaseWWTask.py:789-793 的 wait_login
    已经用 find_boxes 找到了「点击连接」的按钮框、日志里也打印了出来，
    但下一行点的是写死的相对坐标 click_relative(0.503, 0.926) 而不是那个框。
    实测这是失败率最高的一条（8 天里 3 次，每次空点约 100 下直到 10 分钟超时）。
    要修得先有一张卡住时的截图，确认在 3840x2160 下按钮真实落在哪。
    """
    if not images:
        return
    try:
        import os
        import time as _t
        out = os.path.join(os.getcwd(), "logs", "failure_frames")
        os.makedirs(out, exist_ok=True)
        stamp = _t.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c for c in str(who) if c.isalnum() or c in "-_")[:40] or "task"
        # images 可能是 dict{名字: 帧} 或 list[帧]，两种都收
        frames = images.items() if hasattr(images, "items") else enumerate(images)
        for key, frame in frames:
            if frame is None:
                continue
            try:
                import cv2
                path = os.path.join(out, f"{stamp}_{safe}_{key}.png")
                cv2.imwrite(path, frame)
                print(f"[main_chain] 失败画面已存: {path}", flush=True)
            except Exception:
                continue
    except Exception as e:
        print(f"[main_chain] 存失败画面出错（忽略）: {e}", file=sys.stderr, flush=True)


def _close_game_after_failure():
    """任务失败后把游戏关掉。

    为什么要补这个：框架的「Exit After Task」只挂在成功分支上
    （TaskExecutor.py:574 那个 if 在 task.run() 之后的正常路径里），
    异常分支只发通知和截图、不碰游戏进程。于是无人值守时任务一旦失败，
    鸣潮就留在前台满负荷渲染到人起床——实测 Jason 的机器上 8 天里有 3 天
    是这个状态（4 次 `Daily Task exception stopped`），而 2026-07-22/23
    那次 GPU 崩溃本就与显卡负载有关。上游 issue #1589 第 3 条提的正是这件事，
    更早的 #979 因 60 天无回应被自动关闭。

    放在这里而不是信号回调里：回调触发时任务线程还在收尾（发通知、截图、
    disable 任务），当场杀进程会打断那些清理动作，也拿不到完整的失败信息。
    等 OK.start() 返回后再关，此时该记的都记完了。
    """
    try:
        from ok import og
        dm = getattr(og, "device_manager", None)
        if dm is None:
            print("[main_chain] 没拿到 device_manager，跳过关游戏", file=sys.stderr, flush=True)
            return
        # 与成功分支同一个调用（TaskExecutor.py:578）。它内部按 exe 全路径 kill，
        # 只会命中被托管的那个游戏窗口，不会误伤别的进程。
        dm.stop_hwnd()
        print("[main_chain] 任务失败，已关闭游戏", flush=True)
    except Exception as e:
        # 关不掉不该改变退出码——失败判定已经成立了，这只是收尾。
        print(f"[main_chain] 关游戏失败（不影响退出码）: {e}", file=sys.stderr, flush=True)


def main():
    try:
        from config import config
        from ok import OK
        from ok.gui.Communicate import communicate
    except Exception as e:
        print(f"[main_chain] 导入失败: {e}", file=sys.stderr, flush=True)
        return 3

    # 必须在 OK.start() 之前接上：任务是在 start() 里跑的。
    communicate.task_done.connect(_on_task_done)
    communicate.notification.connect(_on_notification)

    try:
        ok = OK(config)
        ok.start()
    except SystemExit as e:
        # OK 内部可能自己 sys.exit（比如 --help、参数错误）。尊重它的意图，
        # 但如果它退的是 0 而我们收到过失败信号，仍然改判为失败。
        code = e.code if isinstance(e.code, int) else 0
        if code == 0 and _failed:
            return 1
        return code
    except Exception as e:
        print(f"[main_chain] 运行期异常: {e}", file=sys.stderr, flush=True)
        return 3

    if _failed:
        _close_game_after_failure()
        return 1
    if not _done:
        # 没有任何任务干净跑完，也没有失败信号——通常是压根没跑起来
        # （游戏窗口没找到、任务索引不对、配置里该任务被禁用等）。
        print("[main_chain] 没有任何任务跑完，也没有失败信号", file=sys.stderr, flush=True)
        _close_game_after_failure()
        return 2
    return 0


def _write_result(rc):
    """把本次运行的结构化结果写成 logs/chain_result.json，给外层调度器读。

    为什么不让外层继续 grep 日志：日志是给人看的，措辞会随版本变（中英文都有），
    grep 判据脆且信息量低——只能知道"成没成"，不知道"哪步、为什么"。
    这份 JSON 是机器对机器的契约：字段稳定、原子写入（先写临时文件再改名）。
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
