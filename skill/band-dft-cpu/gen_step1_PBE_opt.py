#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""band-dft-cpu 技能 step1：PBE 结构优化。

真正的逻辑在公共池 relax_common.py；本文件只声明本技能的策略。
要改通用行为（分段方式、磁性判定、U、ENCUT、模板渲染…）请改公共池，
改这里只影响 band-dft-cpu。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relax_common as R

R.run(
    OUTDIR_SINGLE="step1_PBE_opt",
    SCRIPT_NAME="gen_step1_PBE_opt.py",
    NEXT_STEP="gen_step2_static.py",
    STAGE_MODE="in_job",          # 一个作业内跑完 a->b->c，排一次队
    CELL_POLICY="primitive",      # 能带的高对称路径定义在原胞倒空间
    VACUUM_AXIS_POLICY="rotate",  # 2D 真空不在 c 时自动 3-轮换（与 elastic-dft-cpu/ke-dft-cpu 统一）
    MOL_BRANCH=True,              # 支持 0D 分子（M@C60 这类）
)
