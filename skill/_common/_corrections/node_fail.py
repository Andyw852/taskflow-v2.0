# -*- coding: utf-8 -*-
"""node_fail —— 节点故障 / 被抢占纠错 handler。

现场：diag 含 NODE_FAIL，或 queue.err 里 "NODE FAILURE"；作业还没算完就没了。
处理：retry 重交（与结构、参数无关，纯集群侧问题）。

风险等级 safe，且 auto=True —— 这是最该无人值守自动重交的一类。
"""
from .base import CorrectionHandler, Suggestion


class NodeFailCorrection(CorrectionHandler):
    name = "node_fail"
    title = "节点故障 / 作业被抢占"
    matches = ("node_fail", "node failure", "被抢占", "preempt", "cancelled by slurm")
    steps = ("*",)
    risk = "safe"

    def suggest(self, ctx):
        return Suggestion(
            handler=self.name, title=self.title, risk=self.risk, auto=True,
            reason="作业因节点故障（NODE_FAIL）或抢占退出，与输入无关，直接重交。",
            actions=["retry 重交（保留已有产物）",
                     "若同一材料连续撞 NODE_FAIL ≥3 次：换节点/换 qos/换集群，先请示"],
            commands=[self.retry_cmd(ctx), self.start_cmd(ctx)])


HANDLER = NodeFailCorrection()
