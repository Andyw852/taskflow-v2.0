#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ke-dft-cpu 技能 step1：与 elastic-dft-cpu 同策略（原来两个文件字节相同）。

放置：skill/ke-dft-cpu/step1_opt/gen_step1_std_opt.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relax_common as R

R.run(
    OUTDIR_SINGLE="step1_opt",
    SCRIPT_NAME="gen_step1_std_opt.py",
    NEXT_STEP="gen_step2_static.py",
    STAGE_MODE="in_job",
    CELL_POLICY="primitive",        # 默认原胞（四技能统一）；需惯用晶轴取向(C_ij/输运张量)时，在该项目 step.conf 设 CELL_POLICY=standard
    STD_CELL="primitive_standard",
    VACUUM_AXIS_POLICY="rotate",
    MOL_BRANCH=True,                # step1 支持 0D；AMSET/形变势步骤用 require_dim 拦住
)
