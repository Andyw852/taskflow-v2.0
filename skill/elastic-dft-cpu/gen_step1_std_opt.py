#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""elastic-dft-cpu 技能 step1：标准化(IEEE 取向) + 分段弛豫。

逻辑在公共池 relax_common.py，本文件只声明本技能的策略。
放置：skill/elastic-dft-cpu/gen_step1_std_opt.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relax_common as R

R.run(
    OUTDIR_SINGLE="step1_std_opt",
    SCRIPT_NAME="gen_step1_std_opt.py",
    NEXT_STEP="gen_step2_elastic.py",
    STAGE_MODE="in_job",            # 一个作业内 a->b->c；要恢复"3D 单段"就写 "single"
    CELL_POLICY="primitive",        # 默认原胞（四技能统一）；需惯用晶轴取向(C_ij/输运张量)时，在该项目 step.conf 设 CELL_POLICY=standard
    STD_CELL="primitive_standard",  # 或 "conventional"
    VACUUM_AXIS_POLICY="rotate",    # 2D 真空不在 c 时自动 3-轮换（原 elastic-dft-cpu 行为）
    MOL_BRANCH=True,                # step1 支持 0D；下游 step2_elastic 会用 require_dim 拦住
)
