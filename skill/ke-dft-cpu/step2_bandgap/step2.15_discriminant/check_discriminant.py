#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_discriminant.py —— step2.15 判别静态的收敛检查 + 自动重投

只是收敛检查（退出码约定与其它步骤一致）。**判定落盘由 decide_discriminant.py
负责**（skill.yaml 里 seq 2.155 的分析步，done_marker = discriminant.json），
这样它会被自动调用；本脚本额外打印一次预判，方便人工/agent 立刻看到结论。

注意：杂化步的 ALGO 选择**不依赖** discriminant.json（那会形成循环依赖：
ALGO 要在杂化之前定，而判定要在杂化之后才能含 @杂化 覆盖）。gen_step4_HSE.py
直接读本步的 OUTCAR 现算，读不到就退回 Damped。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "_common" / "opt"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_common import make_argparser, run_static_check, log  # noqa: E402

DEFAULT_DIR = "step2_bandgap/step2.15_discriminant"


def deliverables(job_dir):
    return [("EIGENVAL", 1024), ("IBZKPT", 10), ("OUTCAR", 1024)]


def _preview(args):
    try:
        import discriminant_common as dc
        jd = (Path(args.job_dir).expanduser() if args.job_dir
              else Path(sys.argv[0]).resolve().parent)
        jd = jd.resolve()
        if jd.name != Path(DEFAULT_DIR).name:
            cand = jd / DEFAULT_DIR
            if cand.is_dir():
                jd = cand
        oc = jd / "OUTCAR"
        if not oc.is_file():
            return
        d = dc.decide_from_outcar(oc, dc.read_mesh(jd))
        if d:
            log("[预判] %s@%s  gap=%+.4f eV (mesh %s)  VBM@%s  CBM@%s -> ALGO 建议 %s"
                % (d["label"], d["functional"], d["gap_eV"], d.get("mesh"),
                   d["vbm"]["k_frac"], d["cbm"]["k_frac"],
                   "All" if d["label"] == "SEMICONDUCTOR" else "Damped"))
            if d["label"] != "SEMICONDUCTOR":
                log("[预判] 非 SEMICONDUCTOR：PBE 级判 METAL/SEMIMETAL 是**待定**，"
                    "不是结论（PBE 只会低估带隙）。杂化跑完请用 "
                    "decide_discriminant.py --hybrid-outcar <杂化OUTCAR> 重判。")
    except Exception as e:
        log("[WARN] 预判失败（不影响收敛判定）：%s" % e)


def main():
    args = make_argparser("step2.15 体系判别静态：收敛检查 + 自动重投", DEFAULT_DIR).parse_args()
    _preview(args)
    run_static_check(
        args,
        step_name="step2.15_discriminant",
        default_dir=DEFAULT_DIR,
        deliverables=deliverables,
        archive_keep_outputs=["OUTCAR", "OSZICAR", "queue.out", "queue.err"],
        restart_hint=("已达最大重启次数。本步是普通 PBE 静态，检查：ALGO=Normal->All / "
                      "调小 SIGMA / 结构是否合理。只想拿判定的话，可对已有 OUTCAR 跑 "
                      "decide_discriminant.py"),
    )


if __name__ == "__main__":
    main()
