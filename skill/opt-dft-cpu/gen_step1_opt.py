#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opt-dft-cpu 技能 step1：结构优化（弛豫）。

真正的逻辑在公共池 relax_common.py；本文件只声明本技能的策略，
用 R.run(...) 覆盖 STAGE_SPEC / STAGE_ORDER，只作用于 opt-dft-cpu，
不影响 band-dft-cpu/kl-dft-cpu/ke-dft-cpu/elastic-dft-cpu 的 step1。

★ opt-dft-cpu 的弛豫策略（与其它技能不同）：
  用途是高通量相对稳定性筛选，后面还有 S2 静态收尾，结构精度不必很高，
  力判据统一放到 -0.05（配 EDIFF 1e-4，见 templates/incar_{2d,3d}.tpl）。
  参考：MP 那套 EDIFFG=-0.02 是为把形成能收敛到 ~1 meV/atom；实践上 0.01~0.05 eV/Å
  都算收敛得不错，粗筛/机器学习势/高通量常用 0.05。

  【为什么保留"固定胞预弛豫"这一段——这是速度的关键】
  这些候选结构是生成出来的：Ag 常是手放的猜测位置、笼子也没弛豫过，初始往往离
  极小值差好几 eV。若一上来就 ISIF=3 变胞 CG，原子安顿和晶胞弛豫两组自由度耦合在
  同一个线搜索里，CG 效率很低，会白走几十上百个离子步（实测某结构 77 步还在晃）。
  所以先用一段【固定胞 ISIF=2】把原子（尤其 Ag）便宜地安顿好，再放开胞：
    a  ISIF=2 IBRION=2(CG)  EDIFFG=-0.05  固定胞，先安顿原子（每步 SCF 便宜、无 Pulay）
    b  ISIF=3 IBRION=2(CG)  EDIFFG=-0.05  从 a 的好起点放开胞，收敛快得多
  这样总的"离子步 × 每步 SCF 开销"通常远小于单段变胞硬啃。

  【为什么这里 EARLY_EXIT 关掉】
  提前停(某段收敛就跳过后续)只有在"相邻段同一物理、只是更紧"时才安全。这里 a 是
  固定胞：VASP 的 "reached required accuracy" 只判力不判应力，a 一到 -0.05 就提前停
  会把放开胞的 b 跳过 → 晶胞根本没弛豫。所以带固定胞预弛豫时必须 EARLY_EXIT=False，
  让 b 一定跑。提速靠的是"解耦自由度"，不是提前退出。
  （注：b 若已收敛，本就在第 1 个离子步就停，几乎不浪费。）
  2D 面内变胞约束(IOPTCELL)由公共池在分段前注入；a 段显式去掉它（固定胞），b 段保留。
  0D（Ag@笼 这类）走 mol_common 分子分支，不经过本调度，不受影响。

配套提速（见模板/静态脚本）：
  · 弛豫 LREAL=Auto（大超胞实空间投影，VASP 自己在 log 里建议；粗弛豫精度足够）；
    S2 静态仍用 LREAL=.FALSE. 出精确可比总能。
  · 弛豫 EDIFF=1E-4；静态 EDIFF=1E-4（需发表级绝对形成能时把静态单独收紧到 1E-5~1E-6）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relax_common as R

# ---- opt-dft-cpu 专属：固定胞预弛豫 + 变胞弛豫，力判据统一 -0.05 ----
GEOMOPT_STAGE_SPEC = {
    "a": {"_desc": "固定胞安顿原子（CG），力判据 -0.05",
          "ISIF": "2", "IBRION": "2", "POTIM": "0.2",
          "EDIFFG": "-0.05", "NSW": "80",
          "IOPTCELL": None},                 # 显式去掉面内变胞约束 = 固定胞
    "b": {"_desc": "放开胞弛豫（CG），力判据 -0.05；从 a 的 CONTCAR 接力",
          "ISIF": "3", "IBRION": "2", "POTIM": "0.2",
          "EDIFFG": "-0.05", "NSW": "120"},
}
GEOMOPT_STAGE_ORDER = ["a", "b"]

R.run(
    OUTDIR_SINGLE="step1_opt",
    SCRIPT_NAME="gen_step1_opt.py",
    NEXT_STEP="gen_step2_static.py",
    STAGE_MODE="in_job",                     # 一个作业内顺序跑，只排一次队
    CELL_POLICY="primitive",                 # 保持输入胞（fullerene 网络 / 分子）
    VACUUM_AXIS_POLICY="rotate",             # 2D 真空轴不在 c 时自动 3-轮换
    MOL_BRANCH=True,                         # 支持 0D（Ag@笼 这类包合物）
    # ↓ opt-dft-cpu 专属弛豫调度（覆盖公共池默认的三段 a/b/c）
    STAGE_SPEC=GEOMOPT_STAGE_SPEC,
    STAGE_ORDER=GEOMOPT_STAGE_ORDER,
    EARLY_EXIT_ON_CONVERGENCE=False,         # 带固定胞预弛豫段时必须关，否则会跳过放开胞的 b
)