# -*- coding: utf-8 -*-
"""nsw_continue —— 弛豫步数用尽（NSW 打满但仍在下降）纠错 handler。

现场：diag 含 "force not converged"，探测器结论 relax_nsw / relax_progressing
（能量单调下降但撞到 NSW 上限）。这是**正常截断**，不是算错。处理：retry 续算。

风险等级 safe：只重交、不碰输入文件，所以 tf diagnose 可以放心把它标成
"suggested_action=retry"；tf correct 也不会改任何东西。
"""
from .base import CorrectionHandler, Suggestion


class NswContinueCorrection(CorrectionHandler):
    name = "nsw_continue"
    title = "弛豫步数用尽（NSW 打满，仍在下降）"
    matches = ("force not converged", "relax_nsw", "relax_progressing",
               "步数用尽", "nsw")
    steps = ("S1", "step1", "step2", "S2", "opt")   # 主要出现在优化类步骤
    risk = "safe"

    def suggest(self, ctx):
        return Suggestion(
            handler=self.name, title=self.title, risk=self.risk, auto=True,
            reason="受力未达判据但能量仍在下降 —— 是 NSW 上限截断，续算即可，"
                   "已有 CONTCAR/波函数全部保留。",
            actions=["retry 续算（opt 步自动把 CONTCAR 拷成 POSCAR）",
                     "若连续 3 轮都刚好打满 NSW：考虑把 NSW 调大或放宽力判据"
                     "（改项目 step.conf，改前请示）"],
            commands=[self.retry_cmd(ctx), self.start_cmd(ctx)])


HANDLER = NswContinueCorrection()
