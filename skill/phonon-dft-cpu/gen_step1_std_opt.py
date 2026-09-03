#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kl-dft-cpu 技能 step1：声子/晶格热导流程的结构优化。

放置：skill/kl-dft-cpu/gen_step1_std_opt.py
注意 kl-dft-cpu 原脚本没有作业内分段（只有 TWO_STAGE_2D 开关的雏形），
迁移后统一走 in_job；力常数对结构质量敏感，收尾段的 EDIFFG 建议维持 -0.001。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relax_common as R

R.run(
    OUTDIR_SINGLE="step1_std_opt",
    SCRIPT_NAME="gen_step1_std_opt.py",
    NEXT_STEP="gen_step2_static.py",
    STAGE_MODE="in_job",
    CELL_POLICY="primitive",        # 默认原胞（四技能统一）；需惯用晶轴取向(C_ij/输运张量)时，在该项目 step.conf 设 CELL_POLICY=standard
    STD_CELL="primitive_standard",
    VACUUM_AXIS_POLICY="rotate",
    MOL_BRANCH=True,                # step1 支持 0D；kappa 步骤用 require_dim 拦住
)
