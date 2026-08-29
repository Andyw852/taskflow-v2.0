#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step3_phonon.py —— 2 阶拟合 + 声子谱（phonon-mace-cpu S3），submit 模式。

接力 step2 的位移+力，写 submit.sh 把 symfc 拟合 + 声子谱放到计算节点作业里跑。
产出（作业跑完后）：phonon_summary.json + band-dft-cpu.yaml。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import klmace_common as kc
import stepconf

OUTDIR = "step3_phonon"
STEP = "step3_phonon"
SRC = "step2_disp_force"

SPEC = {
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("cpu", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": (kc.DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (kc.DEFAULT_CONDA_ENV, "str"),
}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    src = cwd / SRC
    if not src.is_dir():
        sys.exit("[ERROR] 找不到 %s（step2 没跑）" % SRC)
    for f in ("POSCAR", "SPOSCAR", "disps.npy", "forces.npy", kc.KL_PARAMS,
              kc.METHOD_FILE):
        if (src / f).is_file():
            shutil.copyfile(str(src / f), str(out / f))
    for f in ("disps.npy", "forces.npy"):
        if not (out / f).is_file():
            sys.exit("[ERROR] %s 缺 %s —— step2 的取力作业没跑完" % (SRC, f))

    here = Path(__file__).resolve().parent
    if not (here / "phonon_fit_driver.py").is_file():
        sys.exit("[ERROR] 缺 phonon_fit_driver.py —— 本步 gen_need 里漏了它？")
    shutil.copyfile(str(here / "phonon_fit_driver.py"), str(out / "phonon_fit_driver.py"))

    tpl = kc.resolve_submit(here, "submit_mace_relax")
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S3phonon"),
                     "CONDA_SH": conf["CONDA_SH"] or kc.DEFAULT_CONDA_SH,
                     "CONDA_ENV": conf["CONDA_ENV"] or kc.DEFAULT_CONDA_ENV,
                     "MACE_CMD": "python phonon_fit_driver.py",
                     "LOG": "phonon.log"})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪（作业跑完写 phonon_summary.json + band-dft-cpu.yaml）"
          % OUTDIR)


if __name__ == "__main__":
    main()
