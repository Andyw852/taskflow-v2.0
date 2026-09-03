#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step3_phonon.py —— phonopy 收力 + fc2 拟合 + 声子谱（提交计算节点）。

接力 step2 的位移+力，写 submit.sh 把 phonopy 收力 + 拟合 + 谱放到计算节点跑。
产出（作业跑完后）：phonon_summary.json + band-dft-cpu.yaml。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kl_common as kc
import stepconf

OUTDIR = "step3_phonon"
STEP   = "step3_phonon"
DISP   = "step2_disp"

SPEC = {
    "FUNC":        ("pbesol", "str"),   # 全局带入，本步不用
    "IMAG_THR":    (0.10, "float"),
    "BAND_POINTS": (51,   "int"),
}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)
    disp = cwd / DISP

    if not (disp / "phonopy_disp.yaml").is_file():
        sys.exit("[ERROR] %s 缺 phonopy_disp.yaml（step2 未生成位移）" % disp)
    if not list(disp.glob("disp-*/vasprun.xml")):
        sys.exit("[ERROR] %s 下无 disp-*/vasprun.xml，位移单点还没算完" % disp)

    for f in ("POSCAR", "SPOSCAR", "phonopy_disp.yaml", kc.KL_PARAMS, kc.METHOD_FILE):
        src = disp / f
        if src.is_file():
            shutil.copyfile(str(src), str(out / f))

    here = Path(__file__).resolve().parent
    if not (here / "phonon_fit_driver.py").is_file():
        sys.exit("[ERROR] 缺 phonon_fit_driver.py —— gen_need 里漏了它？")
    shutil.copyfile(str(here / "phonon_fit_driver.py"), str(out / "phonon_fit_driver.py"))

    tpl = kc.resolve_submit(here, "3d", "submit_fit")
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": "%s-phonon-dft-cpu-S3" % cwd.name})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪（作业跑完写 phonon_summary.json + band-dft-cpu.yaml）"
          % OUTDIR)


if __name__ == "__main__":
    main()
