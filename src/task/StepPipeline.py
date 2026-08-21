import time

from ok import Logger, TaskDisabledException

logger = Logger.get_logger(__name__)

# 步骤四态。照 ok-nte 的 task_status 范式(作者较新的项目, 吸收了教训的写法),
# 用简单字符串而不是枚举类 —— info_set 出去在 GUI/日志里直接可读。
PENDING = 'pending'
RUNNING = 'running'
SUCCESS = 'success'
FAILED = 'failed'
SKIPPED = 'skipped'


class StepPipeline:
    """把任务的 run() 从"一条直线"改成"可配置的步骤流水线"的混入类。

    ## 解决什么

    现状的任务是一大段从头写到尾的过程代码：中间任何一步抛异常, 后面全部跳过;
    出问题时外部只能看到"失败", 看不出卡在哪一步、为什么、重试过没有。
    (2026-08-18 实测: 体力刷完后卡在找宝箱一步, 当天所有领取全部跳过。)

    ## 刻意的设计取舍(为什么不是一个"图引擎")

    - **步骤重试是配置项, 不是引擎机制** —— 每步的重试次数由 default_config 里的
      英文 key 控制, 顺带白拿 GUI 表单与持久化。这是本框架的成文教条
      (.agents/skills/ok-script-tasks/SKILL.md: retry 用 config 值 + for-range)。
    - **步骤超时是"软预算", 不做硬中断** —— 框架是单线程协作式模型(取消靠
      check_enabled 在 next_frame/sleep 里抛异常), 没有安全的办法从外部掐断一个
      正在跑的步骤。假装能硬超时只会做出一个不可靠的止损。所以: 步骤内部的等待
      仍由框架原语(wait_until/wait_ocr 的 time_out)兜底, 流水线只在步骤返回后
      核对耗时、超预算就记警告; 真正的硬止损由外层调度(计划任务的
      ExecutionTimeLimit / 晨链的独立止损)负责 —— 这正是作者自己给无人值守
      场景的既定答案。
    - **失败不中断整体, 但整体结果如实上报** —— 失败的步骤记入 status, 收尾时
      若有 required 步骤失败则抛异常。绝不能吞掉失败安静返回:
      「Successfully Executed Task」是框架在 run() 正常返回后才打的, 外层调度器
      靠它判成败, 吞掉失败会把"半途而废"洗成"完全成功"。

    ## 用法

        class MyTask2(StepPipeline, WWOneTimeTask, BaseCombatTask):
            def run(self):
                WWOneTimeTask.run(self)
                self.pipeline_reset()
                self.step('OpenPage', self.open_page)                # 必需步骤
                self.step('ClaimA', self.claim_a, required=False)    # 可选步骤
                self.step('ClaimB', self.claim_b, required=False,
                          skip_if=lambda: self.already_claimed)      # 条件跳过
                self.pipeline_finish()

    每步的状态经 info_set('Step: <名字>', ...) 实时暴露, GUI 的运行面板和日志里
    都能看到进行到哪一步。
    """

    # 子类可在 default_config 里放同名 key 覆盖(英文 key, 会自动生成 GUI 表单项)
    STEP_RETRY_KEY = 'Step Retry Count'
    STEP_BUDGET_KEY = 'Step Time Budget (s)'

    def pipeline_reset(self):
        """开始一轮流水线。必须在 run() 里步骤开始前调用。"""
        self.step_status = {}
        self.step_elapsed = {}
        self._pipeline_failed_required = []
        self._pipeline_failed_optional = []

    def step(self, name, func, *args, required=True, retry=None, budget=None,
             skip_if=None, recover=True, **kwargs):
        """执行一个步骤。返回该步骤是否成功。

        required: 失败时是否影响整体成败(False = 尽力而为, 失败只记录)。
        retry:    重试次数, None 则读 config 的 Step Retry Count(默认 1, 即最多跑 2 遍)。
        budget:   软时间预算秒数, None 则读 config 的 Step Time Budget (s)(默认 0=不核对)。
        skip_if:  可调用, 返回真则跳过本步骤(记为 skipped)。
        recover:  失败后是否 ensure_main 回主界面, 给下一步一个干净起点。
        """
        if skip_if is not None and skip_if():
            self._set_status(name, SKIPPED)
            logger.info(f'step {name} skipped')
            return True

        if retry is None:
            retry = int(self.config.get(self.STEP_RETRY_KEY, 1))
        if budget is None:
            budget = float(self.config.get(self.STEP_BUDGET_KEY, 0))

        self._set_status(name, RUNNING)
        start = time.time()
        last_error = None
        for attempt in range(retry + 1):
            try:
                func(*args, **kwargs)
                elapsed = time.time() - start
                self.step_elapsed[name] = round(elapsed, 1)
                self._set_status(name, SUCCESS)
                if budget and elapsed > budget:
                    # 超预算只警告不判败: 预算是排查线索("这步为什么变慢了"),
                    # 不是止损线 —— 止损在外层。
                    logger.warning(f'step {name} took {elapsed:.0f}s, over budget {budget:.0f}s')
                return True
            except TaskDisabledException:
                # 用户点了停止/退出, 不是步骤失败, 原样上抛
                raise
            except Exception as e:
                last_error = e
                self.log_error(f'step {name} attempt {attempt + 1}/{retry + 1} failed', e)
                self.screenshot(f'step_{name}_attempt{attempt + 1}')
                if attempt < retry and recover:
                    if not self._recover():
                        # 恢复失败 = 游戏死了/断线, 重试也是白试, 直接跳出重试环
                        break

        self.step_elapsed[name] = round(time.time() - start, 1)
        self._set_status(name, FAILED)
        if required:
            self._pipeline_failed_required.append((name, str(last_error)))
        else:
            self._pipeline_failed_optional.append((name, str(last_error)))
        if recover and not self._recover():
            # 连主界面都回不去, 后面每一步都注定失败。继续硬走只会把快败拖成
            # 每步一次超时的慢磨, 吃掉外层止损里的重试窗口 —— 直接放弃剩余步骤,
            # 把机会留给链层重试。
            raise last_error
        return False

    def pipeline_finish(self, success_message='Task Completed'):
        """收尾: 汇总各步骤状态; 有 required 失败则抛异常(如实上报, 见类注释)。"""
        summary = ', '.join(f'{n}={s}' for n, s in self.step_status.items())
        logger.info(f'pipeline finished: {summary}')
        self.info_set('Pipeline Result', summary)
        if self._pipeline_failed_optional:
            names = ', '.join(n for n, _e in self._pipeline_failed_optional)
            logger.warning(f'optional steps failed (ignored for overall result): {names}')
        if self._pipeline_failed_required:
            detail = '; '.join(f'{n}: {e}' for n, e in self._pipeline_failed_required)
            raise Exception(f'pipeline finished with failed steps: {detail}')
        self.log_info(success_message, notify=True)

    def _set_status(self, name, status):
        self.step_status[name] = status
        self.info_set(f'Step: {name}', status)

    def _recover(self):
        """回主界面。返回是否成功 —— 调用方据此决定继续还是放弃(fail-fast)。

        time_out 用 60 而不是照抄任务开头的 180: 这里是步骤间的恢复, 游戏若还活着
        60 秒足够回到主界面; 回不去基本就是游戏死了/断线, 多等 120 秒只是拖慢失败。
        """
        try:
            self.ensure_main(time_out=60)
            return True
        except TaskDisabledException:
            raise
        except Exception as e:
            logger.warning(f'recover to main failed: {e}')
            return False
