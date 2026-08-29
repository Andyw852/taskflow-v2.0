#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
step2_check_and_resubmit.py — 弹性（IBRION=6）步收敛检查 + 自动重投。

复用 check_common.run_static_check（静态/电子结构类通用驱动）：
  收敛 = 电子自洽达 EDIFF 且 VASP 正常收尾 且 OUTCAR 里已写出 TOTAL ELASTIC MODULI。
额外内容级校验 extra_check：OUTCAR 必须含 "TOTAL ELASTIC MODULI" 块，否则视为未完成
（作业可能在最后一个形变前就挂了）。

给 agent 的约定与 bd 一致：stdout 一行 JSON，stderr 日志；退出码
  0 converged / 10 resubmitted|not_converged(check-only) / 20 running /
  30 max_restarts_exceeded / 40 error。

用法：
    python step2_check_and_resubmit.py                 # 查同级 step2_elastic
    python step2_check_and_resubmit.py /path/step2_elastic --check-only
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_common as cc  # noqa: E402

STEP = "step2_elastic"


def has_elastic_tensor(job_dir: Path):
    """OUTCAR 必须含 TOTAL ELASTIC MODULI 块（IBRION=6 全部形变跑完才会写）。"""
    outcar = job_dir / "OUTCAR"
    if not outcar.exists():
        return False, "OUTCAR 缺失"
    text = outcar.read_text(errors="ignore")
    if "TOTAL ELASTIC MODULI" not in text:
        return False, "OUTCAR 尚无 TOTAL ELASTIC MODULI（有限差分未跑完或中断）"
    return True, ""


def main():
    ap = cc.make_argparser(
        description="弹性 IBRION=6 步收敛检查 + 自动重投", default_dir=STEP)
    args = ap.parse_args()
    cc.run_static_check(
        args,
        step_name=STEP,
        default_dir=STEP,
        deliverables=[("OUTCAR", 1)],
        archive_keep_outputs=["OUTCAR", "queue.out", "queue.err"],
        extra_check=has_elastic_tensor,
        restart_hint="弹性未出张量：查 slurm/OUTCAR 末尾——常见为超时（形变数×SCF 太多，"
                     "调大 walltime 或 KPAR）、SCF 不收敛（改 ALGO/减小 POTIM）、"
                     "或结构未弛豫到位（回 step1 收紧）",
    )


if __name__ == "__main__":
    main()
