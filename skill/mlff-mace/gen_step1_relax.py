#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step1_relax.py —— mlff-mace step1：原胞紧弛豫（VASP，三段式，复用 relax_common）。

与常规 step1 的差别（mlff 数据集的 DFT 基准必须比常规静态更严）：
    - EDIFFG = -0.001（三段都是），NSW 300/300/200
    - 判据（checks.py ck_step1）：收敛 + 末次 external pressure ≤ 2 kB
      （2D 只看面内分量）
    - 输出 workflow_method.txt（FUNC/GGA/IVDW/DIM/MAG/LDAU）+ CONTCAR +
      OUTCAR/EIGENVAL（step4/5 从它们读带隙/磁矩定 ISMEAR/ISPIN）
    - 12 核提交：step.conf [submit] nodes=1 ntasks_per_node=12
      （模板 ntasks-per-node=24 由 apply_submit 覆盖；改核数用 tf conf）

结构来源：材料目录 POSCAR（无上一步，全新体系从这里开始）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relax_common as R

# ---- mlff-mace 专属：紧弛豫（力 -0.001，压强调到 2 kB 以下）----
TIGHT_STAGE_SPEC = {
    "a": {"_desc": "固定胞安顿原子（CG），力判据 -0.001",
          "ISIF": "2", "IBRION": "2", "POTIM": "0.2",
          "EDIFFG": "-0.001", "NSW": "300",
          "IOPTCELL": None},                 # 显式去掉面内变胞约束 = 固定胞
    "b": {"_desc": "放开胞弛豫（CG），力判据 -0.001",
          "ISIF": "3", "IBRION": "2", "POTIM": "0.2",
          "EDIFFG": "-0.001", "NSW": "300"},
    "c": {"_desc": "固定胞收尾（原 ISIF=3 变胞在 hf VASP 6.4.3 上从 primitive 晶格拉成惯用胞体积跑飞触发 FEXCP；b 段已变胞到平衡，c 只精收原子），力判据 -0.001",
          "ISIF": "2", "IBRION": "1", "POTIM": "0.2",
          "EDIFFG": "-0.001", "NSW": "200"},
}

R.run(
    OUTDIR_SINGLE="step1_relax",
    SCRIPT_NAME="gen_step1_relax.py",
    NEXT_STEP="gen_step2_supercell.py",
    STAGE_MODE="in_job",                     # 一个作业内顺序跑 a/b/c，只排一次队
    CELL_POLICY="primitive",                 # 取原胞（超胞/声子都在原胞上展开）
    MOL_BRANCH=False,                        # 0D 孤立分子不支持（要训练超胞）
    STAGE_SPEC=TIGHT_STAGE_SPEC,
    STAGE_ORDER=["a", "b", "c"],
    # ★ 必须 False：阶段 a 是固定胞（ISIF=2），VASP 的收敛判定只判力不判应力，
    #   a 一收敛就提前停会把放开胞的 b 跳过 → 晶胞根本没弛豫 → 压强闸必挂。
    #   三段同一物理、b/c 若已收敛本来就 1 步即停，几乎不浪费。
    EARLY_EXIT_ON_CONVERGENCE=False,
)
