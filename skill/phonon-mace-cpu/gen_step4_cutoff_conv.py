#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step4_cutoff_conv.py —— FC_CUTOFF 收敛性检查（phonon-mace-cpu S5），submit 模式。

接力 step3_phonon 的 POSCAR/disps.npy/forces.npy，写 submit.sh 把 cutoff_conv_driver.py
放到计算节点作业里跑（对 CUTOFF_LIST 每个 cutoff 重拟合，写 cutoff_conv_summary.json）。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import klmace_common as kc
import stepconf

OUTDIR = "step4_cutoff_conv"
STEP = "step4_cutoff_conv"
SRC = "step3_phonon"

SPEC = {
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("cpu", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": (kc.DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (kc.DEFAULT_CONDA_ENV, "str"),
    "CUTOFF_LIST": ("4.0,6.0,8.0", "str"),
}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    # 数据来源（v2：本步允许单独换集群跑）：
    #   1) 同级 step3_phonon/ —— 同集群接力的正常路径；
    #   2) 本目录 —— step3 在别的集群跑过、本步单独挪到新集群时用；
    #      文件由 tf 按 gen_need 从 项目侧 <材料>/<技能>/templates/<本步>/ 推送。
    src = cwd / SRC
    kl_src = src / kc.KL_PARAMS
    for f in ("POSCAR", "disps.npy", "forces.npy", kc.KL_PARAMS, kc.METHOD_FILE):
        p = src / f
        if not p.is_file():
            p = cwd / f
        if p.is_file():
            shutil.copyfile(str(p), str(out / f))
            if f == kc.KL_PARAMS:
                kl_src = p
    for f in ("disps.npy", "forces.npy"):
        if not (out / f).is_file():
            sys.exit("[ERROR] 缺 %s —— 远端 %s/ 没有，本目录也没有 tf 推送的副本"
                     "（请把 step3 产物放到 <材料>/<技能>/templates/%s/）"
                     % (f, SRC, STEP))

    here = Path(__file__).resolve().parent
    if not (here / "cutoff_conv_driver.py").is_file():
        sys.exit("[ERROR] 缺 cutoff_conv_driver.py —— 本步 gen_need 里漏了它？")
    shutil.copyfile(str(here / "cutoff_conv_driver.py"), str(out / "cutoff_conv_driver.py"))

    tpl = kc.resolve_submit(here, "submit_mace_relax")
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S5conv"),
                     "CONDA_SH": conf["CONDA_SH"] or kc.DEFAULT_CONDA_SH,
                     "CONDA_ENV": conf["CONDA_ENV"] or kc.DEFAULT_CONDA_ENV,
                     "MACE_CMD": "python cutoff_conv_driver.py",
                     "LOG": "cutoff_conv.log"})
    inherited = kc.read_kl_params(kl_src)
    inherited["CUTOFF_LIST"] = conf["CUTOFF_LIST"]
    kc.write_kl_params(out / kc.KL_PARAMS, **inherited)
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪（作业跑完写 cutoff_conv_summary.json + band-cut*.yaml）"
          % OUTDIR)


if __name__ == "__main__":
    main()
