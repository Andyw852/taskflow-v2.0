#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
step2_check_and_resubmit.py
===========================
step2_PBE_static（一致性静态 SCF，vasp_std）收敛检查 + 自动重投。

判定：电子自洽达 EDIFF + VASP 正常收尾 + CHGCAR/DOSCAR 产出。
重投：打包留档 -> 清理中间输出 -> 原样重投（ISTART=0/ICHARG=2 冷启动即可）。

约定（与 step1 脚本一致）：
    stdout 只有一行 JSON；stderr 是过程日志。
    退出码 0=converged  10=resubmitted/check-only  20=running
           30=max_restarts_exceeded  40=error

用法：
    python step2_check_and_resubmit.py                # 默认检查同级 step2_PBE_static
    python step2_check_and_resubmit.py /path/to/dir --check-only
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_common import make_argparser, run_static_check  # noqa: E402

DEFAULT_DIR = "step2_PBE_static"

# step2 的关键产物：后续分析要 CHGCAR（LCHARG=.TRUE.）与 DOSCAR/IBZKPT（step3 要用）
DELIVERABLES = [
    ("CHGCAR", 1024),
    ("IBZKPT", 10),
]

# 重投时打包留档的输出
ARCHIVE_KEEP = ["OUTCAR", "OSZICAR", "queue.out", "queue.err"]


def main():
    args = make_argparser(
        "step2 静态 SCF 收敛检查 + 自动重投（供多智能体调用）", DEFAULT_DIR
    ).parse_args()
    run_static_check(
        args,
        step_name="step2_PBE_static",
        default_dir=DEFAULT_DIR,
        deliverables=DELIVERABLES,
        archive_keep_outputs=ARCHIVE_KEEP,
        restart_hint=("已达最大重启次数，建议人工检查：ALGO=Normal->All / "
                      "调小 SIGMA / 检查初始磁矩与结构合理性"),
    )


if __name__ == "__main__":
    main()
