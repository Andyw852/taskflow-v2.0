#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step3_calib.py —— mlff-mace step3：基座模型 CALIB_FC2 → u_rms(300K) → RATTLE_STD。

运行位置：超算登录节点，cwd = 材料目录（run: gen 步骤，不提交 SLURM）。
重活由 fc2_calib.py 在 venv 里干（基座模型取力 ~分钟级）。

读：step1_relax/CONTCAR + step2_supercell/supercell_summary.json + step.conf
写：step3_calib/calib_summary.json（u_rms / RATTLE_STD 三档 / 是否退化 fallback）
退出码 0 成功；非 0 = [ERROR]。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc
import stepconf

OUTDIR = "step3_calib"
STEP = "step3_calib"


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)
    cv = dict(conf.params)

    if not (cwd / "step2_supercell" / "supercell_summary.json").is_file():
        sys.exit("[ERROR] step2_supercell/supercell_summary.json 不存在 —— 先让 S2 跑完。")

    rc = mc.run_py("fc2_calib.py",
                   "--prim step1_relax/CONTCAR "
                   "--sc-summary step2_supercell/supercell_summary.json "
                   "--model '%s' --model-dir '%s' "
                   "--device %s --disp 0.01 "
                   "--fallback '%s' --out %s/calib_summary.json"
                   % (cv["MACE_MODEL"] or "mace-mp:medium",
                      cv["MACE_MODEL_DIR"] or "",
                      (cv["DEVICE"] or "cpu") if cv["DEVICE"] != "cuda" else "cpu",
                      cv["RATTLE_STD_FALLBACK"], OUTDIR),
                   cwd, conf=cv, logname="calib.log")
    if rc != 0:
        sys.exit("[ERROR] fc2_calib.py 失败（rc=%d），看 calib.log 尾部。" % rc)
    print("[DONE] %s/calib_summary.json 已生成" % OUTDIR)


if __name__ == "__main__":
    main()
