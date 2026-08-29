#!/bin/bash
# =============================================================================
# monitor.sh —— 半小时巡检：推进(静默) + 采集 diff(无变化 0 输出)
#
# 用法（crontab，每 30 分钟）：
#   */30 * * * * /home/wangchao/software/taskflow/monitor.sh >> /home/wangchao/software/taskflow/.tf_monitor.out 2>&1
#
# 省 token 的关键：
#   1. tf start 的推进日志静默到 .tf_monitor.log（不刷屏）；
#   2. tf summary --diff 无变化时输出 0 字节，只有状态真变（有作业完成/新失败/
#      排队变化）才打印几行汇总——agent/人只看这最后几行。
# =============================================================================
cd /home/wangchao/software/taskflow || exit 1

# 1) 推进流水线（提交可开始的步骤；FAIL 只报告不动），日志静默
TF_OP_WORKERS=8 timeout 600 tf start >> .tf_monitor.log 2>&1

# 2) 采集 + 变更检测：无变化 0 输出，有变化才打印汇总 + FAIL 清单
TF_OP_WORKERS=8 timeout 600 tf summary --diff 2>&1
