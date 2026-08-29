#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step2_supercell.py —— mlff-mace step2：判维度 + 从基座模型读 r_max 定超胞。

运行位置：超算登录节点，cwd = 材料目录（run: gen 步骤，不提交 SLURM）。
重活（读 .model 的 r_max / 元素表）由 supercell_tool.py 在 venv 里干。

读：step1_relax/CONTCAR（上一步弛豫结构）+ step.conf（MACE_MODEL 等）
写：step2_supercell/supercell_summary.json + mlff_params.txt（DIM/SUPERCELL/RMAX）
退出码 0 成功；非 0 = [ERROR]。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc
import stepconf

OUTDIR = "step2_supercell"
STEP = "step2_supercell"


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)

    if not (cwd / "step1_relax" / "CONTCAR").is_file():
        sys.exit("[ERROR] step1_relax/CONTCAR 不存在 —— 先让 S1_relax 收敛。")
    if not (cwd / "step1_relax" / "workflow_method.txt").is_file():
        sys.exit("[ERROR] step1_relax/workflow_method.txt 不存在。")

    conf_val = dict(conf.params)
    rc = mc.run_py("supercell_tool.py",
                   "--prim step1_relax/CONTCAR "
                   "--model '%s' --model-dir '%s' "
                   "--min-atoms %d --max-atoms %d --min-vacuum %g "
                   "--out %s/supercell_summary.json"
                   % (conf_val["MACE_MODEL"] or "mace-mp:medium",
                      conf_val["MACE_MODEL_DIR"] or "",
                      conf_val["MIN_ATOMS"], conf_val["MAX_ATOMS"],
                      conf_val["MIN_VACUUM"], OUTDIR),
                   cwd, conf=conf_val, logname="supercell.log")
    if rc != 0:
        sys.exit("[ERROR] supercell_tool.py 失败（rc=%d），看 supercell.log 尾部。" % rc)
    print("[DONE] %s/supercell_summary.json 已生成" % OUTDIR)


if __name__ == "__main__":
    main()
