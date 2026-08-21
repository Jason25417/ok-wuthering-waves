from ok import Logger, TaskDisabledException
from src.task.DailyTask import DailyTask, ADDITIONAL_TASKS, AUTO_FARM_NIGHTMARE_NEST
from src.task.ForgeryTask import ForgeryTask
from src.task.NightmareNestTask import NightmareNestTask
from src.task.SimulationTask import SimulationTask
from src.task.TacetTask import TacetTask
from src.task.WWOneTimeTask import WWOneTimeTask
from src.task.StepPipeline import StepPipeline

logger = Logger.get_logger(__name__)


class DailyTask2(StepPipeline, DailyTask):
    """日常任务的流水线版, 与原版并排出现在任务列表里。

    与 DailyTask 的行为对照:
      · 步骤内容完全一致(直接复用父类的 open_daily/claim_* 等方法), 差异只在编排:
        每步独立成败、失败自动重试(次数可配)、状态实时可见、结束时如实汇总。
      · 原版一步失败后面全跳过; 本版失败的步骤记录下来继续走, 收尾时若有必需步骤
        失败仍然抛异常 —— 外层判成败的语义与原版一致, 不会把半途而废洗成成功。

    刻意保留原版不动、新开一个类: 两版并排跑一段时间对比稳定性, 随时可以切回。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "📅 Daily Task 2 (Pipeline)"
        self.description = ("Pipeline edition of Daily Task: same steps, but each step "
                            "retries independently and failures don't skip the rest.")
        # 继承 DailyTask 的全部配置, 只追加流水线自己的两个旋钮(英文 key → 自动 GUI 表单)
        self.default_config = dict(self.default_config)
        self.default_config[self.STEP_RETRY_KEY] = 1
        self.default_config[self.STEP_BUDGET_KEY] = 0
        self.config_description = dict(self.config_description)
        self.config_description[self.STEP_RETRY_KEY] = (
            'How many times to retry a failed step (0 = no retry).')
        self.config_description[self.STEP_BUDGET_KEY] = (
            'Log a warning if a single step takes longer than this many seconds (0 = off).')

    def run(self):
        self.pipeline_reset()
        self.validate_additional_tasks()

        WWOneTimeTask.run(self)
        self.logged_in = False
        # 进不了主界面就没有任何步骤能做, 让它按原语义直接抛(fail-fast),
        # 不算作一个"可跳过的步骤"
        self.ensure_main(time_out=180)

        additional_tasks = self.config.get(ADDITIONAL_TASKS) or []
        condition1 = AUTO_FARM_NIGHTMARE_NEST in additional_tasks
        condition2 = self.config.get('Farm Nightmare Nest for Daily Echo')

        used_stamina, daily_reward_ready = self.open_daily()
        need_stamina = not daily_reward_ready and used_stamina < 180
        need_nightmare = condition1 or (
                condition2
                and not daily_reward_ready
                and self.config.get('Which to Farm', self.support_tasks[0]) != self.support_tasks[0]
        )

        self.step('NightmareNest', self._step_nightmare, condition1,
                  required=False, skip_if=lambda: not need_nightmare)
        self.step('FarmStamina', self._step_stamina, used_stamina,
                  required=True, skip_if=lambda: not need_stamina)

        # 领取类: 互不依赖的免费资源, 尽力而为但如实记录
        self.step('ClaimDaily', self.claim_daily, required=False)
        self.step('ClaimMail', self.claim_mail, required=False)
        self.step('ClaimBattlePass', self.claim_battle_pass, required=False)
        self.step('AdditionalTasks', self.run_additional_tasks, required=False)

        self.pipeline_finish('Daily Task Completed')

    def _step_nightmare(self, farm_all):
        """梦魇分支。劫持/还原 ensure_main 的写法照抄父类 run() 里的原样
        (含 #1322 引入的 __dict__.pop 还原), 只是失败处理交给流水线。"""
        try:
            self.get_task_by_class(NightmareNestTask).ensure_main = lambda *args, **kwargs: None
            if farm_all:
                self.run_task_by_class(NightmareNestTask)
            else:
                self.get_task_by_class(NightmareNestTask).run_capture_mode()
        finally:
            self.get_task_by_class(NightmareNestTask).__dict__.pop('ensure_main', None)

    def _step_stamina(self, used_stamina):
        target = self.config.get('Which to Farm', self.support_tasks[0])
        if target == self.support_tasks[0]:
            self.get_task_by_class(TacetTask).farm_tacet(
                daily=True, used_stamina=used_stamina, config=self.config)
        elif target == self.support_tasks[1]:
            self.get_task_by_class(ForgeryTask).farm_forgery(
                daily=True, used_stamina=used_stamina, config=self.config)
        else:
            self.get_task_by_class(SimulationTask).farm_simulation(
                daily=True, used_stamina=used_stamina, config=self.config)
        self.sleep(4)
