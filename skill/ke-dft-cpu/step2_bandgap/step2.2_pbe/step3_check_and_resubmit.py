#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
step3_check_and_resubmit.py
===========================
step3_PBE_WAVECAR（PBE+SOC 预收敛，vasp_ncl，产出非共线 WAVECAR）
收敛检查 + 自动重投。

判定：电子自洽达 EDIFF + VASP 正常收尾 + WAVECAR 足够大（>1 MB）。
      WAVECAR 是本步的唯一交付物，太小说明作业没跑完或 LWAVE 没开。
重投：打包留档 -> 清理中间输出（含不完整的 WAVECAR）-> 原样重投。

约定（与 step1 脚本一致）：
    stdout 只有一行 JSON；stderr 是过程日志。
    退出码 0=converged  10=resubmitted/check-only  20=running
           30=max_restarts_exceeded  40=error

用法：
    python step3_check_and_resubmit.py                # 默认检查同级 step3_PBE_WAVECAR
    python step3_check_and_resubmit.py /path/to/dir --check-only
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_common import make_argparser, run_static_check  # noqa: E402

DEFAULT_DIR = "step3_PBE_WAVECAR"

# 本步交付物：非共线 WAVECAR（step4 HSE 热启动的输入）。
# step3 也带 KPOINTS_OPT（顺手白拿一条 PBE+SOC 能带做对照），那条能带的本征值
# 【不在 EIGENVAL / EIGENVAL_OPT 里】，而在 vasprun.xml 的 <eigenvalues_kpoints_opt>。
def deliverables(job_dir: Path):
    d = [("WAVECAR", 1024 * 1024),   # >1 MB 才认为有效
         ("EIGENVAL", 100)]
    if (job_dir / "KPOINTS_OPT").exists():
        d.append(("vasprun.xml", 1000))
    return d


def kpoints_opt_done(job_dir: Path):
    """确认 vasprun.xml 里真的写出了 KPOINTS_OPT 的 one-shot 本征值。"""
    if not (job_dir / "KPOINTS_OPT").exists():
        return True, ""
    vr = job_dir / "vasprun.xml"
    if not vr.exists():
        return False, "缺 vasprun.xml —— KPOINTS_OPT 的能带本征值只写在里面"
    try:
        with open(vr, "r", errors="ignore") as f:
            for line in f:
                if "eigenvalues_kpoints_opt" in line:
                    return True, ""
    except OSError as exc:
        return False, f"读 vasprun.xml 失败: {exc}"
    return False, ("vasprun.xml 里没有 <eigenvalues_kpoints_opt> —— "
                   "自洽没收敛完，KPOINTS_OPT 的 one-shot 根本没跑")


ARCHIVE_KEEP = ["OUTCAR", "OSZICAR", "queue.out", "queue.err"]


def check_incar_soc(job_dir: Path):
    """重投前顺带校验 INCAR：SOC 步必须 LWAVE=.TRUE.，否则重投也白跑。"""
    incar = job_dir / "INCAR"
    if not incar.exists():
        return False, "INCAR 缺失，无法重投"
    text = incar.read_text(errors="ignore").upper()
    if "LWAVE" in text and ".FALSE." in text.split("LWAVE", 1)[1].split("\n", 1)[0]:
        return False, "INCAR 中 LWAVE=.FALSE.，本步不会产出 WAVECAR，请先修正"
    return True, ""


def main():
    args = make_argparser(
        "step3 PBE+SOC 预收敛 WAVECAR 检查 + 自动重投（供多智能体调用）", DEFAULT_DIR
    ).parse_args()
    run_static_check(
        args,
        step_name="step3_PBE_WAVECAR",
        default_dir=DEFAULT_DIR,
        deliverables=deliverables,
        archive_keep_outputs=ARCHIVE_KEEP,
        pre_resubmit=check_incar_soc,
        extra_check=kpoints_opt_done,
        restart_hint=("已达最大重启次数，建议人工检查：NCORE 是否为 1（vasp_ncl 要求）/ "
                      "NBANDS 是否过大导致内存不足 / ALGO=Normal->All"),
    )


if __name__ == "__main__":
    main()
