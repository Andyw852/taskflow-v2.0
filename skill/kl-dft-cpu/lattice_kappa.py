# ============================================================================
# amset_kappa.py
# 热导率计算
#
# 本版相对旧版的结构性改动：
#   1) 常调参数集中在顶部 DEFAULTS，进阶/罕调项放 ADVANCED_DEFAULTS（顶部更清爽），
#      run(config) 以 {**DEFAULTS, **ADVANCED_DEFAULTS, **config} 合并，任意键均可传参覆盖。
#   2) INCAR 走「模板继承链」：
#        incar_template（结构优化基底，如 晶格热导率2.tpl）
#          → 解析为 dict
#          → 静态 INCAR = 基底 + static_incar_overrides（继承模板，覆盖部分键）
#          → DFT 取力 INCAR = 静态 + dft_incar_overrides（继承静态，覆盖部分键）
#      覆盖字典中 value=None 表示「删除该键」（如静态删 ISIF / EDIFFG）。
#      ENCUT / KSPACING / KGAMMA / EDIFF / 磁矩 在运行时注入，保证各阶段一致。
#   3) 磁矩开关 magnetic（默认关）+ 磁矩继承（链式：初猜 → relax → static → 超胞）：
#      - 原胞 MAGMOM 由 magmom 配置生成（None=按 magmom_default 填充 /
#        {元素:值} / 逐原子列表，列表对应【标准化后】POSCAR-prim0 的原子顺序）；
#      - magmom_inherit=True 时逐级继承：relax OUTCAR 收敛磁矩 → static 初猜；
#        static OUTCAR 收敛磁矩 → 超胞取力初猜。保证几何与磁性解自洽；
#      - ★球投影校正：OUTCAR magnetization(x) 逐原子值是 PAW 球内投影，
#        丢掉了间隙区磁化、系统性偏低（巡游体系尤甚）。继承时按 OUTCAR 的
#        晶胞总磁矩（number of electron ... magnetization 行，含间隙区）等比
#        放大逐原子值（只放大不缩小，scale 限 [1, cap]）；AFM（Σm≈0）无法
#        归一，统一乘 afm_boost 防止初猜偏低塌成 NM 解；
#      - 超胞 MAGMOM：用「几何映射」(超胞分数坐标×reps mod 1 → 原胞原子)
#        按 eq_supercell_atoms 的真实原子顺序逐原子赋值，hiphive(pymatgen序)
#        与 findiff(phonopy序) 两分支都正确，不会因排序不同而错位。
#   4) 扩胞倍数：显式给 supercell=[a,b,c] 即直接采用；未给则在弛豫后结构上按
#      min_sc_length / min_sc_diameter / max_atoms 自动计算（不再提前返回等确认）。
#
# 力常数生成方法（method 切换）："random"(随机位移,默认) / "findiff"(有限位移)
# NAC（nac 开关，默认关）：极性材料 Γ 点 LO-TO 劈裂，需 LEPSILON DFPT。
# ============================================================================
#
# ★★★ 移植 / 运行须知（务必先看）★★★
# 1) ShengBTE 提交：脚本通过 hpc.submit_shengbte(workdir, exe, config) 提交。
#    你的 hpc.py 里目前只有 submit_vasp，需要照同样风格补一个 submit_shengbte：
#    它在 workdir(=8_kappa，已备好 CONTROL/FORCE_CONSTANTS_2ND/3RD/POSCAR) 里
#    跑 ShengBTE，并【阻塞到算完】才返回（脚本提交后立即读 BTE.KappaTensorVsT_*）。
# 2) shengbte_exe：DEFAULTS 里默认占位 "ShengBTE"，改成你集群上 ShengBTE 可执行文件
#    的实际完整路径（或确保它在 PATH 中）。
# 3) findiff + 热导率：method="findiff" 且 kappa=True 时，走标准 phono3py 主流——
#    步骤5+6 一次性生成「一批 0.03 Å 三阶位移超胞」(4_disp/POSCAR-*)，每个单点 DFT
#    (5_dft/POSCAR-*)；步骤9 从这同一批同时 produce fc2+fc3：fc2 出声子谱+判虚频，
#    fc2+fc3 交步骤10 算 κ。无第二批 DFT、谱与 κ 共用同一个 fc2（与 random 完全对称）。
#    位移数由对称性 + findiff_fc3_cutoff_pair(半径) 决定，非 ALM；被 cutoff 砍掉的槽位
#    在 POSCAR 编号里缺号，步骤8/9 自动据实际目录号回填、并强校验缺帧。
#    大超胞三阶位移数会爆炸，请设 findiff_fc3_cutoff_pair=4~6 Å 控制 DFT 量。
#    （kappa=False 时 findiff 仍走 phonopy 0.01 Å 二阶位移，只出声子谱。）
# ============================================================================

from pathlib import Path
from typing import Dict, Any, List, Optional
import math
import gzip
import re
import shutil
import subprocess
import traceback

import numpy as np
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
# kl9: 可选依赖：jinja2/requests 只被 load_base_incar()/generate_potcar() 用到，
# 而 taskflow 流程（step4 的 alm 分支等）根本不调它们。改成可选依赖，
# 这样没装 jinja2 的集群也能正常 import lattice_kappa。
try:
    import requests
except ImportError:
    requests = None
try:
    from jinja2 import Template
except ImportError:
    Template = None

from ase.io import read as ase_read
from ase.io import write as ase_write
from ase.data import chemical_symbols

from pymatgen.core import Structure
from pymatgen.io.vasp.inputs import Potcar

try:
    import hpc   # 与原脚本一致：hpc.submit_vasp(workdir, config)
except Exception:           # 测试/无集群环境下允许导入；真正提交时才需要
    hpc = None


# ██████████████████████████████████████████████████████████████████████████
# ██  固化参数（流程/数值定义，不暴露给 job config）                          ██
# ██  下方两个字典里的所有键都是「流程的一部分」，不接受 run(config) 覆盖。    ██
# ██  job 调用方仅可通过 config 传入 _ALLOWED_OVERRIDES 白名单里的键（见下）。██
# ██████████████████████████████████████████████████████████████████████████
# 允许 run(config) 透传/覆盖的键。其余键即便出现在 config 里也会被静默忽略，
# 避免误覆盖固化流程参数。
#
# 「标准 job + 标准环境」原则：
#   - 流程数值（位移幅度 / 截断 / 网格 / INCAR 模板 / 迭代次数）一律走 DEFAULTS 固化，不开放；
#   - HPC 执行命令（cmd）、taskId 链路（parentId）、ShengBTE 可执行路径（shengbte_exe）
#     由标准镜像/控制平面提供，不暴露；
#   - 调用方只能动三类：① 输入结构（必填）；② 材料属性决定的物理开关；
#     ③ HPC 资源规模（节点数 / 进程数 / 内存）。
_ALLOWED_OVERRIDES = frozenset({
    # ── 输入（必填）──
    "inputfile",
    # ── HPC 资源规模（按结构大小调）──
    "n_nodes", "tasks_per_node", "mem",
    # ── 材料级物理开关（材料属性决定，调用方知道结构）──
    "method",        # "random" / "findiff"：力常数法
    "kappa_solver",  # "shengbte" / "phono3py"：BTE 求解器
    "magnetic",      # 是否自旋极化
    "nac",           # 是否做 NAC 非解析项修正（极性材料 LO-TO 劈裂）
    "skip_nac_if_metal",  # 检测到金属是否自动跳过 NAC（默认 True；材料属性决定）
    "magmom",        # 初始磁矩配置（None / {元素:值} / 逐原子列表）
    "min_sc_length", "min_sc_diameter", "max_atoms",
    "supercell",           # 显式扩胞倍数 [a,b,c]；给定即用，跳过推荐；None=弛豫后自动计算
    # ── 平台注入的链路字段（透传给 hpc.submit 作 parentId）──
    "job.parentId",
})

DEFAULTS: Dict[str, Any] = {

    # ── 流程开关 ────────────────────────────────────────────────────────────
    "method":     "random",    # 力常数法："random"(随机位移,文献名) / "findiff"(有限位移)
    "relax":      True,        # 是否先做 ISIF=3 结构优化
    "nac":        False,       # 是否做 NAC 非解析项修正（极性材料 LO-TO 劈裂）
    "skip_nac_if_metal": True, # ★金属自动跳过 NAC：检测到金属(带隙≈0)时强制关 NAC 并提醒。
                               #   金属自由载流子屏蔽宏观纵场 → 无 LO-TO 劈裂；且 DFPT 介电
                               #   (LEPSILON)对金属发散/不适定。设 False 可强制保留用户 nac 设置。
    "metal_gap_thr":     0.01, # 判金属的带隙阈值(eV)：弛豫/静态 vasprun 带隙 < 此值判为金属
    "magnetic":   False,       # ★自旋极化总开关（默认关）；开启则写 ISPIN+MAGMOM
    "cell_type":  "primitive", # 工作晶胞："primitive" / "conventional"

    # ── DFT 帧失败容错 ───────────────────────────────────────────────────────
    #   微扰帧数量多，个别帧可能因节点崩/超时/未收敛而失败。此三项控制「失败重算 +
    #   容忍跳过」策略。★物理约束：random 可跳过失败帧（fc 是回归拟合，少几帧只是样本
    #   少）；findiff 不可跳（每帧对应一个对称不等价位移，缺帧无法有限差分重建 fc，
    #   步骤9 会硬报错）。平衡帧则任何方法下都必须算完（力常数零点参考），否则整批力失基准。
    "dft_retry":          1,     # 失败/未完成微扰帧的自动重算轮数(0=纯跳过,不重算);每轮只重投未完成帧
    "min_success_ratio":  0.8,   # random:成功帧占比下限,低于则报错(避免用过少帧硬拟合);findiff 不看此项(必须齐全)
    "min_success_frames": 0,     # random:成功帧绝对下限(0=不启用,仅用比例判据)

    # ── 磁矩（magnetic=True 时生效）─────────────────────────────────────────
    "magmom":         None,    # None=按元素查内置磁矩表 DEFAULT_MAGMOM_TABLE 填充 / {元素:值} / [逐原子]
    "magmom_default": 0.6,     # 兜底初猜(μB)：仅用于「不在内置磁矩表里」的元素(如 O/Se 等非磁元素)；
                               #   常见磁性元素(Fe/Co/Ni/Mn/Cr/稀土…)已由内置表直接给大初猜，
                               #   特殊体系仍可用 magmom={"Fe":4,...} 覆盖个别元素。
    "magmom_inherit": True,    # 逐级继承收敛磁矩作初猜：relax OUTCAR→static，static OUTCAR→超胞
                               #   （继承时自动做球投影校正，阈值固化在 MAG_CAL_* 常量，不开放）
    "ispin":          2,       # magnetic 时写入 ISPIN
    "lorbit":         11,      # magnetic 时写入 LORBIT（输出逐原子磁矩到 OUTCAR）

    # ── INCAR 模板 + 各阶段覆盖（继承链：base模板=relax → static → dft）──────
    #   base 模板「就是结构优化 INCAR」，其内所有标签直接进工作流；relax 直接用它，
    #   static / dft 在其上继承并覆盖。仅 ENCUT、MAGMOM 由工作流运行时自动注入，
    #   其余一律以模板字面值为准（改模板即改流程）。覆盖字典 value=None 表示删键。
    "incar_template": "tpl/INCAR.base",
    # ★超胞取力 INCAR 来源开关（默认 False=走上面的 base→static→dft 继承链）：
    #   True  时步骤7「超胞 DFT 单点取力」绕过继承链，直接读 supercell_incar_template
    #         作为 INCAR（ENCUT/MAGMOM 仍自动注入，模板里增删标签原样进 INCAR）；
    #   False 时维持原行为（dft = base + static_incar_overrides + dft_incar_overrides）。
    "use_supercell_incar_template": False,
    "supercell_incar_template":     "tpl/INCAR.supercell",
    "relax_incar_overrides": {},                # base 模板即 relax，无需额外覆盖
    "static_incar_overrides": {                 # 静态：继承 base，关闭离子弛豫
        "IBRION": -1, "NSW": 0, "ISIF": None, "EDIFFG": None, "ADDGRID": None,
    },
    "nac_incar_overrides": {                    # nac=True 时叠加到静态（DFPT 介电+Born）
        "LEPSILON": ".TRUE.", "LPEAD": ".TRUE.",
        "NPAR": None, "NCORE": None,            # 与 LEPSILON 不兼容，移除
    },
    "dft_incar_overrides": {                    # 超胞单点取力：继承静态，无额外离子弛豫
        "IBRION": -1, "NSW": 0, "ISIF": None, "EDIFFG": None, "LREAL": ".FALSE.",
    },

    # ── ENCUT（工作流注入；其余电子结构精度全部见 INCAR 模板）──────────────
    #   ENCUT = max(ENMAX) × encut_scale，relax/static/dft 全程一致。
    #   1.3× 同时抑制 ISIF=3 的 Pulay/基组不完备，故单次弛豫即可（无需变胞重启）。
    #   是否做结构优化由顶部 relax 开关控制（relax=False 则完全跳过）。
    "encut":       None,   # 绝对值(eV)：设定则全程强制此值，忽略 encut_scale
    "encut_scale": 1.3,    # ENCUT = max(ENMAX) × 此值（建议 1.3，保守可设 1.5）

    # ── 超胞 / 对称 ─────────────────────────────────────────────────────────
    "supercell":         None,   # ★显式扩胞倍数 [a,b,c]（int）。给定即直接采用（跳过推荐，
                                 #   min_sc_length/diameter/max_atoms 仅作事后核查 warning 不再约束）；
                                 #   None=弛豫后按下三项自动计算
    "min_sc_length":   10.0,
    "min_sc_diameter": 8.0,
    "max_atoms":       600,
    "symprec_std":     1e-5,     # 标准化原胞 symprec
    "symprec_path":    1e-3,     # 声子谱高对称路径 symprec

    # ── hiphive 随机位移法（仅留常调项；其余进阶项见下方 ADVANCED_DEFAULTS）──
    "hiphive_disp":      0.03,   # 随机位移幅度(每原子 |Δr| RMS, Å)：kappa=True 用（含三阶，需 0.03）
    "hiphive_disp_band": 0.01,   # 随机位移幅度：kappa=False 用（只出声子谱，二阶谐性 0.01 即可）
    "n_struct_min":     4,       # 微扰结构数下限
    "cutoff_margin":    0.1,     # 二阶安全截断 margin (Å)
    "fit_method":       "lasso", # trainstation 拟合方法
    "rotational_rules": ["Huang", "Born-Huang"],  # 旋转不变性求和规则（保证声学支在Γ正确归零）

    # ── findiff 有限位移法 ──────────────────────────────────────────────────
    "findiff_disp":     0.01,    # 二阶(声子谱)有限位移幅度 (Å)；谐性 0.01 即可
    "findiff_fc3_disp": 0.03,    # 三阶(phono3py 热导率)有限位移幅度 (Å)；fc3 需 0.03，勿用 0.01
    #   三阶位移超胞数 = phono3py 对称约化的「双原子位移」对数（纯有限差分，不走 ALM）。
    #   findiff_fc3_cutoff_pair：两个被位移原子的距离上限 (Å)，用来削减三阶位移超胞数；
    #   None=全对称集（最准但最贵，大超胞会爆炸）；大超胞建议 4~6 Å。超出该距离的 fc3 元素置零。
    "findiff_fc3_cutoff_pair": 5.0,

    # ── 声子谱 ──────────────────────────────────────────────────────────────
    "imag_thr":    0.5,          # 虚频阈值 (THz)
    "band_npoints": 101,         # 每段 q 点数
    "nac_factor":  14.399652,    # VASP NAC 单位因子 (eV·Å)

    # ── 热导率（kappa）────────────────────────────────────────────────────────
    #   声子谱无虚频后自动计算晶格热导率。fc2+fc3 都在步骤9 从「同一批位移」产出，
    #   步骤10 直接复用，零额外 DFT。fc3 来源随 method：
    #     random  → rattle 0.03 力直接拟合 fc2+fc3
    #     findiff → phono3py 0.03 单批 produce fc2+fc3（与声子谱共用同一 fc2）
    #   求解器由 kappa_solver 选：
    #     phono3py → 对 random / findiff 都支持
    #     shengbte → 仅 random（findiff 的 compact fc3 无可靠 ShengBTE 导出口，会报错）
    "kappa":          True,         # 是否计算热导率（关掉则只出声子谱）
    "kappa_solver":   "shengbte",   # "shengbte"(仅 random) / "phono3py"(random+findiff)
    "fc3_cutoff":     5.0,          # hiphive fc3 ClusterSpace 截断 (Å)；None=超胞安全上限
    "kappa_mesh":     [20, 20, 20], # BTE q 网格（phono3py mesh / ShengBTE ngrid）
    "kappa_t_min":    100.0,
    "kappa_t_max":    800.0,
    "kappa_t_step":   100.0,
    "kappa_isotope":  True,         # 自然同位素散射
    "kappa_nac":      None,         # None=随全局 nac；True/False 显式覆盖
    # phono3py 专属
    "kappa_wigner":      True,      # Wigner 相干项 κ_C（模间相干，C60网络/无序合金不可忽略）
    "kappa_cutoff_freq": 0.01,      # 软模截止频率 (THz)
    # shengbte 专属
    "kappa_scalebroad":  0.1,       # 高斯展宽因子
    "kappa_convergence": True,      # 迭代 BTE（CONV），否则仅 RTA
    "shengbte_exe":      "ShengBTE",# ShengBTE 可执行路径（按集群修改）

    # ── 输入/输出 ───────────────────────────────────────────────────────────
    "input_dir": "input",
    "tpl_dir":   "tpl",
}
# ██████████████████████████████████████████████████████████████████████████


# ── hiphive 进阶项（一般保持默认即可；需要时仍可在 run(config) 中覆盖）──────
#   从主 DEFAULTS 移下来只为让顶部更清爽，行为完全不变：run() 会把本字典
#   一并合并进 C，所以脚本里 C["mc_n_iter"] 等访问照常工作，也照常可被 config 覆盖。
ADVANCED_DEFAULTS: Dict[str, Any] = {
    "mc_dmin_scale":  0.85,    # d_min = 最近邻 × 此系数
    "mc_n_iter":      10,      # MC-rattle 每原子迭代次数
    "oversample":     5,       # ALM 过采样系数（微扰结构数 ≈ 自由参数 × 此值）
    "alm_tolerance":  1e-3,    # ALM 对称容差
    "mc_cal_max":     5,       # rattle_std 标定最大迭代
    "mc_cal_tol":     0.08,    # 标定收敛容差（位移幅度比值）
    "mc_cal_probe":   8,       # 标定探针帧数
    "seed":           2025,    # 随机种子（复现用）
    "fit_train_size": 1.0,     # trainstation 训练集比例
}
# ██████████████████████████████████████████████████████████████████████████


# 模板缺失时的内置默认 INCAR 基底（仅电子结构通用项；阶段差异由覆盖字典负责）
# 模板缺失时的内置兜底（与交付的 tpl/INCAR.base 内容一致）：完整 relax 基底
DEFAULT_INCAR_TEMPLATE = """SYSTEM   = relax
PREC     = Accurate
ENCUT    = 520
EDIFF    = 1E-6
EDIFFG   = -1E-2
IBRION   = 2
ISIF     = 3
NSW      = 200
NELMIN   = 5
ISMEAR   = 0
SIGMA    = 0.05
KSPACING = 0.377
KGAMMA   = .TRUE.
LREAL    = .FALSE.
LASPH    = .TRUE.
ADDGRID  = .FALSE.
LWAVE    = .FALSE.
LCHARG   = .FALSE.
"""

# 超胞静态「取力」模板的内置兜底（与交付的 tpl/INCAR.supercell 内容一致）：
#   仅在 use_supercell_incar_template=True 且模板文件缺失时使用。
DEFAULT_SUPERCELL_INCAR_TEMPLATE = """SYSTEM   = supercell_force
PREC     = Accurate
ENCUT    = 520
EDIFF    = 1E-7
IBRION   = -1
NSW      = 0
NELM     = 120
NELMIN   = 5
ISMEAR   = 0
SIGMA    = 0.05
KSPACING = 0.377
KGAMMA   = .TRUE.
LREAL    = Auto
LASPH    = .TRUE.
LWAVE    = .FALSE.
LCHARG   = .FALSE.
"""

ROOT = Path("output")
SUBDIRS = {
    "relax":     ROOT / "0_relax",
    "static":    ROOT / "1_static",
    "supercell": ROOT / "2_supercell",
    "alm":       ROOT / "3_alm",
    "rattle":    ROOT / "4_disp",
    "dft":       ROOT / "5_dft",
    "fcs":       ROOT / "6_fcs",
    "band-dft-cpu":      ROOT / "7_band",
    "kappa":     ROOT / "8_kappa",     # 步骤10 晶格热导率（findiff 与 random 都零额外 DFT）
}

ANG2BOHR = 1.0 / 0.529177249
VALID_METHODS = ("random", "findiff")


def init_dirs():
    # kappa 由热导率步骤按需创建；kappa=False 时不留空目录
    for name, d in SUBDIRS.items():
        if name in ("kappa",):
            continue
        d.mkdir(parents=True, exist_ok=True)
    logging.info("目录已就绪: %s", ROOT.resolve())


def _dft_dir_complete(d: Path) -> bool:
    """位移/平衡帧目录是否正常算完。判据:OUTCAR(.gz) 存在且含 VASP 结束标志
    'General timing and accounting'(节点崩/超时被杀则无此标志 → 判为未完成)。"""
    outcar = d / "OUTCAR"
    if outcar.is_file():
        try:
            size = outcar.stat().st_size
            if size == 0:
                return False
            with open(outcar, "rb") as f:
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", "replace")
            return "General timing and accounting" in tail
        except Exception:
            return False
    gz = d / "OUTCAR.gz"
    if gz.is_file():
        try:
            with gzip.open(str(gz), "rt", encoding="utf-8", errors="replace") as f:
                return "General timing and accounting" in f.read()
        except Exception:
            return False
    return False


# ============================================================================
# INCAR 模板解析 / 合并 / 序列化（继承链核心）
# ============================================================================

def parse_incar_text(text: str) -> Dict[str, str]:
    """INCAR 文本 -> 有序 dict {KEY(大写): value_str}，忽略注释/空行；支持 ; 分隔。"""
    d: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split("!", 1)[0].strip()
        if not line:
            continue
        for part in line.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            d[k.strip().upper()] = v.strip()
    return d


def merge_incar(base: Dict[str, str], overrides: Dict[str, Any]) -> Dict[str, str]:
    """在 base 上叠加 overrides：value=None 表示删除该键；其余覆盖/新增。"""
    d = dict(base)
    for k, v in (overrides or {}).items():
        K = k.strip().upper()
        if v is None:
            d.pop(K, None)
        else:
            d[K] = str(v)
    return d


def incar_dict_to_text(d: Dict[str, str], system: str = "calc") -> str:
    out = [f"SYSTEM = {system}"]
    for k, v in d.items():
        if k == "SYSTEM":
            continue
        out.append(f"{k} = {v}")
    return "\n".join(out) + "\n"


def load_base_incar(C: Dict[str, Any], formula: str = "", encut: float = 0.0,
                    tpl_path: Optional[Any] = None,
                    fallback: Optional[str] = None) -> Dict[str, str]:
    """读取并渲染 INCAR 模板 -> dict。模板缺失则用兜底字符串。
    默认读 C['incar_template']（base 模板）；传 tpl_path/fallback 可读其它模板
    （如超胞静态 tpl/INCAR.supercell），供超胞取力「绕过继承链」直接用模板。"""
    tpl_path = Path(tpl_path) if tpl_path is not None else Path(C["incar_template"])
    fallback = fallback if fallback is not None else DEFAULT_INCAR_TEMPLATE
    ctx = {
        "formula": formula, "inputfile": "", "system": "phonon",
        "ENCUT": int(encut) if encut else 520, "encut": int(encut) if encut else 520,
        "KSPACING": C.get("kspacing", 0.377),
        "KGAMMA": ".TRUE." if C.get("kgamma", True) else ".FALSE.",
        "EDIFF": f"{C.get('ediff', 1e-6):.0e}", "NAT": 0,
    }
    if tpl_path.is_file():
        raw = tpl_path.read_text(encoding="utf-8")
        try:
            if Template is None:                 # kl9: 没装 jinja2 -> 走下面的剥离分支
                raise ImportError("jinja2 未安装")
            text = Template(raw.lstrip("\n")).render(**ctx)
        except Exception as e:
            logging.warning("模板渲染失败(%s)，剥离 Jinja 占位行后解析", e)
            text = "\n".join(l for l in raw.splitlines()
                             if "{{" not in l and "{%" not in l)
    else:
        logging.warning("INCAR 模板 %s 不存在，使用内置默认基底", tpl_path)
        text = fallback
    base = parse_incar_text(text)
    logging.info("INCAR 模板 %s 解析得到 %d 个键: %s", tpl_path.name, len(base), ", ".join(base))
    return base


def finalize_incar(C: Dict[str, Any], base: Dict[str, str], overrides: Dict[str, Any],
                   encut: float, system: str,
                   moments: Optional[List[float]] = None,
                   symbols: Optional[List[str]] = None,
                   extra: Optional[Dict[str, Any]] = None) -> str:
    """base -> 叠加 overrides(及 extra) -> 注入 ENCUT(+磁矩) -> INCAR 文本。
    KSPACING/EDIFF/ISMEAR 等其余标签一律取自模板，本函数不强制覆盖。"""
    d = merge_incar(base, overrides)
    if extra:
        d = merge_incar(d, extra)
    d = merge_incar(d, {"ENCUT": f"{encut:.0f}"})
    if C["magnetic"]:
        spin: Dict[str, Any] = {"ISPIN": C["ispin"], "LORBIT": C["lorbit"]}
        if moments is not None:
            spin["MAGMOM"] = magmom_to_incar_string(moments, symbols=symbols)
        d = merge_incar(d, spin)
    return incar_dict_to_text(d, system=system)


# ============================================================================
# 磁矩工具（初始生成 / OUTCAR 继承 / 超胞几何映射）
# ============================================================================

# 常见磁性元素「初猜」磁矩表 (μB)，谱系沿用 Materials Project / pymatgen 习惯并做实用扩充。
#   注意：这是 SCF 的「起步初猜」而非物理收敛值，故对 3d/4f 磁体宁高勿低（VASP 自旋会向下弛豫，
#   起步太小易塌成非磁解；起步偏大几乎总能正确收敛）。收敛后的真实磁矩以 OUTCAR 为准
#   （magmom_inherit=True 会自动继承）。表外元素回退到 magmom_default。
DEFAULT_MAGMOM_TABLE: Dict[str, float] = {
    # —— 3d 过渡金属 ——
    "Sc": 1.0, "Ti": 1.0, "V": 3.0, "Cr": 5.0, "Mn": 5.0, "Fe": 5.0,
    "Co": 4.0, "Ni": 2.0, "Cu": 1.0,
    # —— 4d / 5d 常见磁性 ——
    "Mo": 5.0, "Ru": 1.0, "Rh": 1.0, "W": 5.0,
    # —— 4f 稀土（局域大磁矩）——
    "Ce": 1.0, "Pr": 2.0, "Nd": 3.0, "Sm": 5.0, "Eu": 7.0, "Gd": 7.0,
    "Tb": 6.0, "Dy": 5.0, "Ho": 4.0, "Er": 3.0, "Tm": 2.0, "Yb": 1.0,
}


def build_primitive_moments(prim_atoms, magmom_cfg, default: float) -> List[float]:
    """生成原胞逐原子初始磁矩（顺序与 prim_atoms 一致 = 写出的 POSCAR 顺序）。
    取值优先级：用户 magmom（dict 显式值）> 内置 DEFAULT_MAGMOM_TABLE（常见磁性元素）> default 兜底。
      · magmom=None      -> 逐元素查表，表外用 default；
      · magmom={元素:值} -> 该元素用用户值，未列元素再走「表 -> default」；
      · magmom=[逐原子]  -> 直接按列表赋值（长度须=原胞原子数）。"""
    syms = prim_atoms.get_chemical_symbols()

    def per_elem(s: str) -> float:               # 元素级默认：内置表优先，兜底 default
        return float(DEFAULT_MAGMOM_TABLE.get(s, default))

    if magmom_cfg is None:
        moms = [per_elem(s) for s in syms]
    elif isinstance(magmom_cfg, dict):
        moms = [float(magmom_cfg[s]) if s in magmom_cfg else per_elem(s) for s in syms]
    elif isinstance(magmom_cfg, (list, tuple, np.ndarray)):
        if len(magmom_cfg) != len(syms):
            raise ValueError(f"magmom 逐原子列表长度 {len(magmom_cfg)} != 原胞原子数 {len(syms)}")
        moms = [float(x) for x in magmom_cfg]
    else:
        raise ValueError(f"magmom 仅支持 None / dict / list，收到 {type(magmom_cfg)}")
    logging.info("[磁矩] 原胞初始 MAGMOM(%d 原子): %s", len(moms),
                 magmom_to_incar_string(moms, symbols=syms))
    return moms


def magmom_to_incar_string(moments: List[float], ndec: int = 2,
                           symbols: Optional[List[str]] = None) -> str:
    """逐原子磁矩 -> VASP 压缩写法。给 symbols 时在元素边界断开便于核对，
    如 '1*4.2 2*0.0 4*0.0'（Mn|In|Se）；不给则纯按数值合并，如 '1*4.2 6*0.0'。"""
    n = len(moments)
    if not n:
        return ""
    vals = [round(float(m), ndec) for m in moments]
    syms = list(symbols) if symbols is not None else [None] * n
    out, run_v, run_s, run_n = [], vals[0], syms[0], 1
    for i in range(1, n):
        if vals[i] == run_v and syms[i] == run_s:
            run_n += 1
        else:
            out.append(f"{run_n}*{run_v}")
            run_v, run_s, run_n = vals[i], syms[i], 1
    out.append(f"{run_n}*{run_v}")
    return " ".join(out)


# ── 继承磁矩「球投影低估」校正的固化常量（内部实现细节，不进 config）─────────
#   OUTCAR magnetization(x) 逐原子值 = PAW 球内投影，缺间隙区贡献、系统性偏低。
MAG_CAL_NOISE    = 0.05   # |m| 低于此值(μB)视为投影数值噪声，清零
MAG_CAL_CAP      = 1.5    # FM 按总磁矩归一的 scale 上限（防 Σm 近零时爆掉）
MAG_AFM_BOOST    = 1.2    # AFM（Σm≈0 无法归一）统一放大系数，防塌 NM
MAG_COLLAPSE_THR = 0.3    # 初猜有磁而继承后 max|m| 低于此值(μB) → 疑似塌缩 warning


def _total_mag_from_outcar_text(text: str) -> Optional[float]:
    """OUTCAR 每步 SCF 打印的晶胞总磁矩（含间隙区，即真实值），取最后一次：
       'number of electron  100.00000 magnetization  16.00000'。无则 None。"""
    ms = re.findall(r"number of electron\s+[-\d.Ee+]+\s+magnetization\s+(-?[\d.Ee+]+)", text)
    try:
        return float(ms[-1]) if ms else None
    except ValueError:
        return None


def calibrate_inherited_moments(moms: List[float], m_tot: Optional[float],
                                noise: float = MAG_CAL_NOISE,
                                afm_boost: float = MAG_AFM_BOOST,
                                cap: float = MAG_CAL_CAP) -> List[float]:
    """球内投影磁矩 -> 校正后的 MAGMOM 初猜（方案 A）。
    magnetization(x) 逐原子值是 PAW 球内投影，缺间隙区、系统性偏低；直接作初猜
    有把临界磁矩带塌成 NM 解的风险（初猜只需「序 + 量级」正确，宁高勿低）。
      · FM/亚铁磁（|Σm| 足够大）：按晶胞总磁矩 m_tot（含间隙区）等比放大，
        scale = clip(m_tot/Σm, 1.0, cap) —— 只放大不缩小；
      · AFM（|Σm|≈0 无法归一）或 m_tot 缺失：统一乘 afm_boost；
      · |m| < noise 视为投影噪声清零（避免非磁位点带无意义小初猜）。"""
    moms = [0.0 if abs(m) < noise else float(m) for m in moms]
    if not any(moms):
        return moms                          # 全零 = NM 解，无需校正
    s = sum(moms)
    if m_tot is not None and abs(s) > 0.5:   # FM/亚铁磁：可按总磁矩归一
        scale = min(cap, max(1.0, m_tot / s))
    else:                                    # AFM / 总磁矩缺失：固定安全放大
        scale = afm_boost
    if abs(scale - 1.0) > 1e-3:
        logging.info("[磁矩] 球投影校正 scale=%.3f（Σm_proj=%.3f, m_tot=%s）",
                     scale, s, f"{m_tot:.3f}" if m_tot is not None else "N/A")
    return [m * scale for m in moms]


def read_magmoms_from_outcar(outcar_path: Path, n_expected: Optional[int] = None,
                             calibrate: bool = True) -> Optional[List[float]]:
    """从 OUTCAR 最后一个 magnetization (x) 块读逐原子总磁矩（tot 列）。无则返回 None。
    calibrate=True（默认）时按方案 A 做「球投影→总磁矩归一」校正
    （阈值见 MAG_CAL_* 常量与 calibrate_inherited_moments）。"""
    if not outcar_path.is_file():
        gz = Path(str(outcar_path) + ".gz")
        if gz.is_file():
            outcar_path = gz
        else:
            logging.warning("[磁矩] OUTCAR 不存在（%s），无法继承收敛磁矩；"
                            "请确认 HPC 端已回传 OUTCAR", outcar_path)
            return None
    open_fn = gzip.open if str(outcar_path).endswith(".gz") else open
    with open_fn(str(outcar_path), "rt", encoding="utf-8", errors="replace") as f:
        text = f.read()
    starts = [m.end() for m in re.finditer(r"magnetization \(x\)", text)]
    if not starts:
        return None
    seg = text[starts[-1]:]
    moments: List[float] = []
    state = "seek_header"   # seek_header -> seek_dash -> rows
    for ln in seg.splitlines():
        s = ln.strip()
        if state == "seek_header":
            if s.startswith("# of ion"):
                state = "seek_dash"
            continue
        if state == "seek_dash":
            if s and set(s) <= set("-"):
                state = "rows"
            continue
        # rows
        if (s and set(s) <= set("-")) or s.startswith("tot"):
            break
        parts = s.split()
        if len(parts) >= 2 and parts[0].isdigit():
            try:
                moments.append(float(parts[-1]))
            except ValueError:
                break
        else:
            break
    if not moments:
        logging.warning("[磁矩] %s 中未解析到 magnetization(x) 逐原子块"
                        "（检查 ISPIN=2 / LORBIT 是否生效），放弃继承", outcar_path.name)
        return None
    if n_expected is not None and len(moments) != n_expected:
        logging.warning("[磁矩] OUTCAR 读出 %d 个磁矩，与期望 %d 不符，放弃继承",
                        len(moments), n_expected)
        return None
    logging.info("[磁矩] 从 %s 读出球投影磁矩: %s", outcar_path.name,
                 magmom_to_incar_string(moments))
    if calibrate:
        moments = calibrate_inherited_moments(moments, _total_mag_from_outcar_text(text))
        logging.info("[磁矩] 校正后继承初猜: %s", magmom_to_incar_string(moments))
    return moments


def expand_magmom_to_supercell(prim_atoms, prim_moments: List[float],
                               sc_atoms, reps, tol: float = 0.25) -> List[float]:
    """按几何把原胞逐原子磁矩映射到超胞（对角超胞，任意原子排序均正确）。
    超胞分数坐标 × reps mod 1 -> 原胞分数坐标，按 (元素 + MIC 最近) 匹配原胞原子。
    适用于 hiphive(pymatgen序) 与 findiff(phonopy序) 两种 eq_supercell_atoms 顺序。"""
    reps = np.asarray(reps, dtype=float)
    prim_frac = prim_atoms.get_scaled_positions(wrap=True)
    prim_sym = np.array(prim_atoms.get_chemical_symbols())
    sc_frac = sc_atoms.get_scaled_positions(wrap=True)
    sc_sym = np.array(sc_atoms.get_chemical_symbols())

    out: List[float] = []
    for fr, s in zip(sc_frac, sc_sym):
        pf = (fr * reps) % 1.0
        best_j, best_d = -1, 1e9
        for j in np.where(prim_sym == s)[0]:
            diff = pf - prim_frac[j]
            diff -= np.round(diff)           # MIC
            dd = float(np.linalg.norm(diff))
            if dd < best_d:
                best_d, best_j = dd, j
        if best_j < 0:
            raise RuntimeError(f"超胞原子(元素 {s})在原胞中无同种原子，磁矩映射失败")
        if best_d > tol:
            logging.warning("[磁矩] 超胞原子与原胞最近匹配距离 %.3f(分数)偏大，结构可能非严格对角超胞", best_d)
        out.append(float(prim_moments[best_j]))
    logging.info("[磁矩] 超胞 MAGMOM(%d 原子): %s", len(out),
                 magmom_to_incar_string(out, symbols=list(sc_sym)))
    return out


# ============================================================================
# 结构 / 超胞（通用）
# ============================================================================

def read_input_structure(search_dir="input"):
    for pattern in ["*.cif", "POSCAR", "CONTCAR"]:
        hits = sorted(Path(search_dir).glob(pattern))
        if hits:
            atoms = ase_read(str(hits[0]))
            logging.info("读取结构: %s (%s, %d atoms)",
                         hits[0], atoms.get_chemical_formula('metal'), len(atoms))
            return atoms
    raise FileNotFoundError(f"{search_dir} 下未找到 *.cif / POSCAR / CONTCAR")


def standardize_cell(atoms, cell_type="primitive", symprec=1e-5):
    from pymatgen.io.ase import AseAtomsAdaptor
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    struct = AseAtomsAdaptor.get_structure(atoms)
    sga = SpacegroupAnalyzer(struct, symprec=symprec)
    spg_sym, spg_num = sga.get_space_group_symbol(), sga.get_space_group_number()
    if cell_type == "conventional":
        std, tag = sga.get_conventional_standard_structure(), "惯用胞 (conventional)"
    elif cell_type == "primitive":
        std, tag = sga.get_primitive_standard_structure(), "原胞 (primitive)"
    else:
        raise ValueError(f"cell_type 仅支持 'primitive'/'conventional'，收到: {cell_type!r}")
    out = AseAtomsAdaptor.get_atoms(std)
    out.set_pbc(True)
    logging.info("空间群 = %s (No.%d), 标准化为%s: %s, %d atoms",
                 spg_sym, spg_num, tag, out.get_chemical_formula('metal'), len(out))
    return out


def compute_supercell_reps(atoms, min_length=10.0, min_diameter=8.0, max_atoms=600):
    cell = np.asarray(atoms.cell)
    lengths = np.linalg.norm(cell, axis=1)
    V_prim = abs(np.linalg.det(cell))
    perp = np.empty(3)
    for i in range(3):
        j, k = [x for x in range(3) if x != i]
        perp[i] = V_prim / np.linalg.norm(np.cross(cell[j], cell[k]))
    reps_len = [int(math.ceil(min_length / lengths[i])) for i in range(3)]
    reps_diam = [int(math.ceil(min_diameter / perp[i])) for i in range(3)]
    reps = [max(1, reps_len[i], reps_diam[i]) for i in range(3)]
    n_atoms = len(atoms) * int(np.prod(reps))
    while n_atoms > max_atoms and max(reps) > 1:
        def safe_to_reduce(i):
            r = reps[i] - 1
            return r >= 1 and r * lengths[i] >= min_length and r * perp[i] >= min_diameter
        cands = [i for i in range(3) if reps[i] > 1]
        safe = [i for i in cands if safe_to_reduce(i)]
        pool = safe if safe else cands
        cur_len = [reps[i] * lengths[i] for i in range(3)]
        i = max(pool, key=lambda i: cur_len[i])
        reps[i] -= 1
        n_atoms = len(atoms) * int(np.prod(reps))
    sc_len = [reps[i] * lengths[i] for i in range(3)]
    sc_perp = [reps[i] * perp[i] for i in range(3)]
    insphere = min(sc_perp)
    logging.info("扩胞 min_length=%.1f, min_diameter=%.1f -> reps=%s, 内切球=%.2f Å, 原子数=%d",
                 min_length, min_diameter, tuple(reps), insphere, n_atoms)
    if min(sc_len) < min_length:
        logging.warning("受 max_atoms=%d 限制，最短边 %.2f < min_length %.1f", max_atoms, min(sc_len), min_length)
    if insphere < min_diameter:
        logging.warning("受 max_atoms=%d 限制，内切球 %.2f < min_diameter %.1f，二阶截断可能偏小",
                        max_atoms, insphere, min_diameter)
    return tuple(reps), n_atoms


def validate_user_supercell(atoms, reps_raw, min_length, min_diameter, max_atoms):
    """校验用户显式指定的扩胞倍数并做尺寸核查（只 warning 不拦截——用户指定为最高优先级）。
    返回 (reps_tuple, n_atoms)。"""
    try:
        reps = tuple(int(r) for r in reps_raw)
    except (TypeError, ValueError):
        raise ValueError(f"supercell 须为 3 个正整数的列表，如 [2,2,2]；收到 {reps_raw!r}")
    if len(reps) != 3 or any(r < 1 for r in reps):
        raise ValueError(f"supercell 须为 3 个正整数（对角倍数），收到 {reps_raw!r}")
    cell = np.asarray(atoms.cell)
    lengths = np.linalg.norm(cell, axis=1)
    V = abs(np.linalg.det(cell))
    perp = np.array([V / np.linalg.norm(np.cross(cell[j], cell[k]))
                     for i in range(3) for (j, k) in [[x for x in range(3) if x != i]]])
    n_atoms = len(atoms) * int(np.prod(reps))
    sc_len = [reps[i] * lengths[i] for i in range(3)]
    insphere = min(reps[i] * perp[i] for i in range(3))
    logging.info("[扩胞] 用户指定 reps=%s：边长=%s Å, 内切球=%.2f Å, 原子数=%d",
                 reps, [f"{x:.2f}" for x in sc_len], insphere, n_atoms)
    if min(sc_len) < min_length:
        logging.warning("[扩胞] 最短边 %.2f Å < min_sc_length %.1f Å，二阶力常数可能欠收敛",
                        min(sc_len), min_length)
    if insphere < min_diameter:
        logging.warning("[扩胞] 内切球 %.2f Å < min_sc_diameter %.1f Å，截断半径受限",
                        insphere, min_diameter)
    if n_atoms > max_atoms:
        logging.warning("[扩胞] 超胞 %d 原子 > max_atoms %d，DFT 开销将显著增大",
                        n_atoms, max_atoms)
    return reps, n_atoms


# 超胞原子数达 max_atoms 此比例即预警（弛豫后、超胞 DFT 前提示体量，不拦截）
SC_NEAR_CAP_FRAC = 0.9


def supercell_size_summary(atoms, reps):
    """从 (原胞, reps) 算超胞尺寸明细，供弛豫后、超胞 DFT 前统一打印。
    返回 dict：edges(3 边长 Å) / insphere(内切球直径 Å) / n_atoms。"""
    cell = np.asarray(atoms.cell)
    lengths = np.linalg.norm(cell, axis=1)
    V = abs(np.linalg.det(cell))
    perp = [V / np.linalg.norm(np.cross(cell[j], cell[k]))
            for i in range(3) for (j, k) in [[x for x in range(3) if x != i]]]
    reps = [int(r) for r in reps]
    edges = [round(reps[i] * lengths[i], 2) for i in range(3)]
    insphere = round(min(reps[i] * perp[i] for i in range(3)), 2)
    n_atoms = len(atoms) * int(np.prod(reps))
    return {"edges": edges, "insphere": insphere, "n_atoms": n_atoms}


def make_supercell_phonopy(prim_atoms, reps, outfile):
    """用 phonopy 生成对角超胞，原子顺序 = phonopy/phono3py 内部约定。
    保证 rattle→DFT→hiphive→声子谱→phono3py/ShengBTE 全链同序，避免 pymatgen
    铺胞次序（沿 c 最快变化）与 κ 求解器内部重建超胞（沿 a 最快变化）错位
    导致力常数贴错原子（详见 BUG_超胞原子序错位.md：Si κ 从 ~130 → ~10 W/m·K）。
    phonopy 超胞序仅由 unit + supercell_matrix(对角) 决定，与 primitive_matrix 无关，
    故 _solve_phono3py 用同一 unit + supercell_matrix 时两处超胞序天然一致。"""
    from phonopy import Phonopy
    from phonopy.structure.atoms import PhonopyAtoms
    from phonopy.interface.vasp import write_vasp
    unit = PhonopyAtoms(symbols=prim_atoms.get_chemical_symbols(),
                        scaled_positions=prim_atoms.get_scaled_positions(),
                        cell=prim_atoms.cell[:])
    ph = Phonopy(unit, supercell_matrix=np.diag(reps), primitive_matrix="auto")
    write_vasp(str(outfile), ph.supercell)
    sc_atoms = ase_read(str(outfile), format="vasp")
    logging.info("phonopy 超胞已写出（phono3py 同序）: %s  原子数: %d", outfile, len(sc_atoms))
    return sc_atoms


# ============================================================================
# DFT 输入：POTCAR / ENCUT
# ============================================================================

def generate_potcar(poscar_path, potcar_path):
    logging.info("请求 POTCAR 生成服务: %s", poscar_path)
    with open(poscar_path, "rb") as _fh:
        if requests is None:                     # kl9: 没装 requests
            raise ImportError("generate_potcar 需要 requests；"
                              "taskflow 用 vaspkit 生成 POTCAR，不该走到这里")
        resp = requests.post(
            "http://calc-task.ustc.edu:30082/pytasker/api/v1/generator/potcar",
            params={"functional": "PBE"},
            files={"file": ("POSCAR", _fh)},
            proxies={"http": None, "https": None},
        )
    resp.raise_for_status()
    potcar_path.write_bytes(resp.content)
    logging.info("POTCAR 已保存: %s", potcar_path)


def encut_from_potcar(potcar_path):
    potcar = Potcar.from_file(str(potcar_path))
    return max(p.enmax for p in potcar)


# ============================================================================
# 弛豫 / 静态 VASP 运行（接收已渲染好的 INCAR 文本）
# ============================================================================

def run_vasp_relax(prim, relax_dir, incar_text, config):
    relax_dir.mkdir(parents=True, exist_ok=True)
    poscar = relax_dir / "POSCAR"
    ase_write(str(poscar), prim, format="vasp", direct=True, sort=False)
    generate_potcar(poscar, relax_dir / "POTCAR")
    (relax_dir / "INCAR").write_text(incar_text, encoding="utf-8")
    logging.info("relax INCAR 已写出: %s", relax_dir / "INCAR")

    logging.info("  弛豫提交: %s", relax_dir)
    hpc.submit_vasp(str(relax_dir), config)
    contcar = relax_dir / "CONTCAR"
    if not contcar.is_file() or contcar.stat().st_size == 0:
        raise RuntimeError(f"弛豫未产生有效 CONTCAR: {contcar}")

    relaxed = ase_read(str(contcar), format="vasp")
    relaxed.set_pbc(True)
    logging.info("结构优化完成 -> 弛豫原胞 %s, %d atoms",
                 relaxed.get_chemical_formula('metal'), len(relaxed))
    return relaxed


def run_vasp_static(prim, static_dir, incar_text, config, nac=False):
    static_dir.mkdir(parents=True, exist_ok=True)
    poscar = static_dir / "POSCAR"
    ase_write(str(poscar), prim, format="vasp", direct=True, sort=False)
    potcar = static_dir / "POTCAR"
    generate_potcar(poscar, potcar)
    (static_dir / "INCAR").write_text(incar_text, encoding="utf-8")
    logging.info("static%s INCAR 已写出: %s", "(NAC/LEPSILON)" if nac else "", static_dir / "INCAR")

    logging.info("  静态%s计算提交: %s", "(NAC/LEPSILON)" if nac else "", static_dir)
    hpc.submit_vasp(str(static_dir), config)
    vasprun = static_dir / "vasprun.xml"
    if nac and (not vasprun.is_file() or vasprun.stat().st_size == 0):
        raise RuntimeError(f"NAC 计算未产生 vasprun.xml: {vasprun}")
    logging.info("静态计算完成%s", "（含 Born/介电，供 NAC）" if nac else "")
    return vasprun


# ============================================================================
# hiphive 随机位移法专用
# ============================================================================

def write_alm_suggest_input(atoms, orders, cutoffs_ang, outfile, prefix="alm", tolerance=1e-3):
    """ALM suggest 输入。orders=[2] 仅二阶；[2,3] 二阶+三阶。
    cutoffs_ang 与 orders 等长，每阶截断 (Å)，None=不截断。"""
    assert orders == list(range(2, 2 + len(orders))), "orders 须为 [2] 或 [2,3] 这种连续形式"
    assert len(cutoffs_ang) == len(orders), "cutoffs 数量须与 orders 一致"
    norder = len(orders)
    numbers = atoms.numbers
    unique = sorted(set(numbers))
    symbols = [chemical_symbols[z] for z in unique]
    kd_index = {z: i + 1 for i, z in enumerate(unique)}
    cell = np.array(atoms.cell)
    a0_ang = np.linalg.norm(cell[0])
    factor_bohr = a0_ang * ANG2BOHR
    lat_norm = cell / a0_ang
    spos = atoms.get_scaled_positions(wrap=True)
    cut_strs = ["None" if c is None else f"{c * ANG2BOHR:.4f}" for c in cutoffs_ang]

    L = ["&general", f"  PREFIX = {prefix}", "  MODE = suggest",
         f"  NAT = {len(atoms)}; NKD = {len(unique)}",
         f"  KD = {' '.join(symbols)}", f"  TOLERANCE = {tolerance:.1e}", "/", ""]
    L += ["&interaction", f"  NORDER = {norder}", "/", ""]
    L += ["&cutoff"]
    for i, a in enumerate(symbols):
        for b in symbols[i:]:
            L.append(f"  {a}-{b} " + " ".join(cut_strs))
    L += ["/", ""]
    L += ["&cell", f"  {factor_bohr:.10f}"]
    for v in lat_norm:
        L.append(f"  {v[0]:20.15f} {v[1]:20.15f} {v[2]:20.15f}")
    L += ["/", ""]
    L += ["&position"]
    for z, p in zip(numbers, spos):
        L.append(f"  {kd_index[z]} {p[0]:20.16f} {p[1]:20.16f} {p[2]:20.16f}")
    L += ["/", ""]
    outfile.write_text("\n".join(L))
    logging.info("alm.in 已写出: %s (NORDER=%d, cutoffs=%s Bohr)", outfile, norder, cut_strs)
    return outfile


def run_alm(workdir, infile="alm.in", logfile="alm.log", timeout=600):
    alm_exe = shutil.which("alm")
    assert alm_exe is not None, "未找到 alm 可执行文件, 请将其加入 PATH"
    log_path = workdir / logfile
    result = subprocess.run([alm_exe, infile], cwd=str(workdir),
                            capture_output=True, text=True, timeout=timeout)
    log_path.write_text(result.stdout + "\n=== STDERR ===\n" + result.stderr)
    if "Job finished" not in result.stdout:
        raise RuntimeError(f"ALM 运行异常, 请检查日志: {log_path}")
    logging.info("ALM 运行完成, 日志: %s", log_path)
    return log_path


def alm_nfree_via_api(atoms, orders, cutoffs_ang):
    """kl10: ALM Python API：不依赖 alm 可执行文件，直接用 Python API 取各阶自由(不可约)力常数个数。

    返回 {order: nfree}，键与 parse_alm_nfree 一致（2=二阶, 3=三阶）。
    装不了/调不通就抛异常，由调用方回落到命令行路径。

    阶数映射：ALM 的 maxorder=len(orders)（1=只到二阶, 2=到三阶）；
             ALM 的 fc_order = order - 1（1=二阶, 2=三阶）。
    """
    import numpy as _np
    from alm import ALM as _ALM

    maxorder = len(orders)
    nkd = len(set(atoms.numbers))
    # cutoff_radii shape=(maxorder, nkd, nkd)；负值 = 不设截断
    cut = _np.zeros((maxorder, nkd, nkd), dtype=float)
    for i, c in enumerate(cutoffs_ang):
        cut[i] = -1.0 if c is None else float(c)

    with _ALM(_np.array(atoms.cell), atoms.get_scaled_positions(),
              atoms.get_atomic_numbers(), verbosity=0) as _a:
        _a.define(maxorder, cutoff_radii=cut)
        _a.suggest()
        out = {o: int(_a._get_number_of_irred_fc_elements(o - 1)) for o in orders}
    logging.info("ALM(Python API) 自由参数: %s", out)
    return out


def parse_alm_nfree(log_path, orders):
    """解析各阶自由力常数个数，返回 {order: nfree}。"""
    content = log_path.read_text()
    keyword = {2: "HARMONIC", 3: "ANHARM3", 4: "ANHARM4"}
    out = {}
    for order in orders:
        kw = keyword[order]
        m = re.search(rf"Number of\s+free\s+{kw}\s+FCs\s*:\s*(\d+)", content)
        if m is None:
            m = re.search(rf"Number of\s+{kw}\s+FCs\s*:\s*(\d+)", content)
        if m is None:
            raise RuntimeError(f"alm.log 中未找到 {kw}(order {order}) 自由参数量")
        out[order] = int(m.group(1))
    return out


def estimate_n_struct(nfree_by_order, n_atoms_sc, oversample, n_min=10):
    """kl11: 合并各阶 nfree 再整体取整：N = ceil(Σnfree/(3*N_sc)) * oversample，不低于 n_min。

    随机位移的每一帧同时给各阶提供方程（一次取力填满所有阶），所以未知数应按
    总数 Σnfree 计、方程数按 帧数×DOF 计，合并算一次即可。旧式"各阶分别取整
    再相加"会重复计数、系统性高估帧数。
    n_min=10：高对称体系（如金刚石 Si）ALM 反推出的帧数会很小（个位数），
    随机位移太少 symfc 拟合不稳，抬个下限保底。
    """
    dof = 3 * n_atoms_sc
    nfree_total = sum(nfree_by_order.values())
    n_struct = max(n_min, math.ceil(nfree_total / dof) * oversample)
    logging.info("各阶自由参数=%s (合计=%d), DOF=%d, OVERSAMPLE=%d -> N_STRUCT=%d",
                 nfree_by_order, nfree_total, dof, oversample, n_struct)
    return n_struct


def nn_distance(atoms):
    from ase.neighborlist import neighbor_list
    for rc in (3.0, 4.0, 6.0, 8.0, 12.0):
        d = neighbor_list("d", atoms, rc)
        if len(d):
            return float(d.min())
    raise RuntimeError("无法确定最近邻距离")


def generate_calibrated_mc_rattle(atoms, n_struct, target_rms, dmin_scale,
                                  n_iter, seed, max_cal=5, tol=0.08, n_probe_cap=8):
    from hiphive.structure_generation import generate_mc_rattled_structures
    ref = atoms.get_positions()
    nn = nn_distance(atoms)
    d_min = dmin_scale * nn
    logging.info("[MC-rattle] 最近邻=%.3f -> d_min=%.3f (scale=%.2f), 目标位移RMS=%.4f Å",
                 nn, d_min, dmin_scale, target_rms)
    rattle_std = target_rms / math.sqrt(max(3 * n_iter, 1))
    n_probe = min(n_struct, n_probe_cap)
    ratio = float("nan")
    for it in range(max_cal):
        probe = generate_mc_rattled_structures(atoms, n_probe, rattle_std, d_min,
                                               seed=seed, n_iter=n_iter)
        mags = np.concatenate([np.linalg.norm(a.get_positions() - ref, axis=1) for a in probe])
        rms = float(np.sqrt(np.mean(mags ** 2)))
        ratio = rms / target_rms
        logging.info("  [标定 %d] rattle_std=%.5f -> RMS=%.4f (比值 %.2f)", it + 1, rattle_std, rms, ratio)
        if abs(ratio - 1.0) <= tol:
            break
        rattle_std /= ratio
    else:
        logging.warning("  标定 %d 次后比值仍 %.2f，按当前 rattle_std 继续", max_cal, ratio)
    final = generate_mc_rattled_structures(atoms, n_struct, rattle_std, d_min,
                                           seed=seed + 1, n_iter=n_iter)
    mags = np.concatenate([np.linalg.norm(a.get_positions() - ref, axis=1) for a in final])
    fin_rms, fin_max = float(np.sqrt((mags ** 2).mean())), float(mags.max())
    logging.info("[MC-rattle] 生成 %d 帧: RMS=%.4f, max|Δr|=%.4f, rattle_std=%.5f",
                 len(final), fin_rms, fin_max, rattle_std)
    if fin_max > 0.5 * nn:
        logging.warning("  max|Δr|=%.3f 超最近邻一半，简谐假设或失效，建议调小 hiphive_disp", fin_max)
    return final, rattle_std, fin_rms


# ============================================================================
# NAC 提取
# ============================================================================

def _parse_born_epsilon_vasprun(vasprun_path):
    """官方推荐：用 VasprunxmlExpat 直接从 vasprun.xml 解析 Born 有效电荷、
    高频介电张量 ε∞ 与单胞（unitcell）。返回 (borns, epsilon, unitcell)。
    born/epsilon 解析为空是最常见的“读不到 NAC”根因（LEPSILON 未真正生效 /
    被并行静默禁用 / LPEAD 冲突 / 该 vasprun 并非 DFPT 介电计算），交由上层判空。"""
    import io
    from phonopy.interface.vasp import VasprunxmlExpat
    with io.open(str(vasprun_path), "rb") as f:
        vr = VasprunxmlExpat(f)
        vr.parse()
    borns = getattr(vr, "born", None)
    epsilon = getattr(vr, "epsilon", None)
    unitcell = getattr(vr, "cell", None)
    borns = None if borns is None else np.asarray(borns, dtype=float)
    epsilon = None if epsilon is None else np.asarray(epsilon, dtype=float)
    return borns, epsilon, unitcell


def extract_nac_params(vasprun_path, phonon, factor, symprec=1e-5):
    """从【单独的 DFPT 静态 vasprun】读 Born 有效电荷 + ε∞，对称化并约化到
    phonon.primitive，返回 nac_params dict。任一环节缺数据 / 数据不物理即 raise,
    交由上层 except 退回【无 NAC】出谱。

    读取策略（对齐 phonopy 官方 VASP 文档，修复原先读不到 NAC 的问题）：
      ① 首选 VasprunxmlExpat 直接解析 born/epsilon/unitcell，再用
         symmetrize_borns_and_epsilon 按 primitive/supercell 矩阵约化到原胞。
         此时 primitive/supercell 矩阵相对【vasprun 自身的 unitcell】，可避免
         老接口 get_born_vasprunxml 因单胞不一致而读到空 Born，进而在
         elaborate_borns_and_epsilon 里触发 'num_atom N != len(borns) 0' 断言
         崩溃（phonopy issue #420）。
      ② 若 ① 不可用（老版本 phonopy 等）则回退 get_born_vasprunxml。"""
    from pathlib import Path

    # ① 文件缺失 → 放弃 NAC
    if not vasprun_path or not Path(str(vasprun_path)).is_file():
        raise FileNotFoundError(f"NAC vasprun 不存在: {vasprun_path}")

    n_prim = len(phonon.primitive)
    borns = epsilon = None

    try:
        from phonopy.structure.symmetry import symmetrize_borns_and_epsilon
        raw_born, raw_eps, unitcell = _parse_born_epsilon_vasprun(vasprun_path)
        # 显式判空：读不到 Born/ε∞ 时给出明确原因，而不是让下游断言崩溃
        if raw_born is None or raw_born.size == 0:
            raise ValueError("vasprun 未解析到 Born 有效电荷（LEPSILON 未生效 / "
                             "被并行静默禁用 / LPEAD 冲突 / 非 DFPT 介电计算）")
        if raw_eps is None or raw_eps.size == 0:
            raise ValueError("vasprun 未解析到高频介电张量 ε∞")
        n_born = int(len(raw_born))
        n_ucell = 0 if unitcell is None else int(len(unitcell))
        if unitcell is None or n_born != n_ucell:
            raise ValueError(f"Born 数 {n_born} ≠ vasprun 单胞原子数 {n_ucell}，无法对称化")
        borns, epsilon = symmetrize_borns_and_epsilon(
            raw_born, raw_eps, unitcell,
            primitive_matrix=phonon.primitive_matrix,
            supercell_matrix=phonon.supercell_matrix,
            symprec=symprec)
        logging.info("NAC 读取：VasprunxmlExpat + symmetrize_borns_and_epsilon")
    except Exception as e_primary:
        logging.info("VasprunxmlExpat 路径失败（%s），回退 get_born_vasprunxml", e_primary)
        from phonopy.interface.vasp import get_born_vasprunxml
        borns, epsilon, _ = get_born_vasprunxml(
            str(vasprun_path),
            primitive_matrix=phonon.primitive_matrix,
            supercell_matrix=phonon.supercell_matrix,
            is_symmetry=False,            # 全原胞每原子 Born,而非对称独立子集
        )

    borns = np.asarray(borns, dtype=float)
    epsilon = np.asarray(epsilon, dtype=float)

    # ② 空/原子数校验(赋值前 raise,绝不污染 phonon)
    if borns.size == 0:
        raise ValueError("Born 有效电荷解析为空，NAC 不可用")
    if borns.shape[0] != n_prim:
        raise ValueError(f"Born 原子数 {borns.shape[0]} ≠ 原胞原子数 {n_prim}")

    # ③ 数值有效性:含 NaN/Inf → 放弃
    if not (np.all(np.isfinite(borns)) and np.all(np.isfinite(epsilon))):
        raise ValueError("NAC 数据含 NaN/Inf,判为无效")

    # ④ ε∞ 物理性:≈单位阵 ⇒ DFPT 介电其实没算出(未设 LEPSILON / 未收敛 / 被并行静默禁用)
    eps_diag = np.diag(epsilon)
    if np.max(np.abs(epsilon - np.eye(3))) < 5e-2:
        raise ValueError(f"ε∞≈单位阵 (对角={[round(float(x),3) for x in eps_diag]}),"
                         "DFPT 介电未有效计算,判为 NAC 不可用")
    if np.any(eps_diag < 1.0):        # 高频介电对角 <1 非物理
        raise ValueError(f"ε∞ 对角含 <1 非物理值: {[round(float(x),3) for x in eps_diag]}")

    # ⑤ Born 整体≈0 ⇒ 无有效电荷,NAC 无意义
    if np.max(np.abs(borns)) < 1e-2:
        raise ValueError("Born 有效电荷整体≈0,判为无效")

    nac = {"born": borns, "dielectric": epsilon, "factor": factor}
    logging.info("NAC 提取成功: Born %s (全原胞), ε∞ 对角=%s", borns.shape,
                 [round(float(x), 3) for x in eps_diag])
    return nac


# ============================================================================
# 金属判据（带隙 ≈ 0 ⇒ 金属；金属对 NAC/LO-TO 劈裂无物理意义，需跳过）
# ============================================================================

def detect_is_metal(vasprun_path, gap_thr: float = 0.01):
    """从 vasprun.xml 读带隙判断是否金属。
    返回 (is_metal, gap_eV)：
      · 判定成功 → (True/False, 带隙值)；
      · 无法判定(文件缺失 / 解析失败) → (None, None)，调用方据此保持原 nac 设置。
    物理背景：金属的自由载流子屏蔽宏观纵向电场，Born 有效电荷 / LO-TO 劈裂无意义，
    且 DFPT 介电(ε∞)对金属发散/不适定，故金属一律跳过 NAC。
    判据：eigenvalue_band_properties 得到的带隙 < gap_thr(默认 0.01 eV) 即判为金属。"""
    vp = Path(str(vasprun_path)) if vasprun_path else None
    if vp is None or not vp.is_file() or vp.stat().st_size == 0:
        logging.warning("[金属判据] vasprun 缺失/为空(%s)，无法判定金属/绝缘体", vp)
        return None, None
    try:
        from pymatgen.io.vasp.outputs import Vasprun
        vr = Vasprun(str(vp), parse_dos=False, parse_projected_eigen=False,
                     parse_potcar_file=False, exception_on_bad_xml=False)
        gap = float(vr.eigenvalue_band_properties[0])   # (gap, cbm, vbm, is_direct)
    except Exception as e:
        logging.warning("[金属判据] 解析 %s 失败，无法判定金属/绝缘体：%s", vp, e)
        return None, None
    is_metal = gap < float(gap_thr)
    logging.info("[金属判据] band-dft-cpu gap = %.4f eV → %s（阈值 %.3f eV）",
                 gap, "金属" if is_metal else "非金属(绝缘体/半导体)", float(gap_thr))
    return is_metal, gap


# ============================================================================
# 声子谱 + 虚频判断（Plotly 出图，q 路径对任意布拉维格子安全）
# ============================================================================

def _clean_label(s):
    s = s.replace("$", "")
    s = re.sub(r"\\mathrm\{([^}]*)\}", r"\1", s)
    for k, v in {r"\Gamma": "Γ", r"\Sigma": "Σ", r"\Delta": "Δ", r"\Lambda": "Λ",
                 r"\Pi": "Π", r"\Theta": "Θ", r"\Omega": "Ω"}.items():
        s = s.replace(k, v)
    return s.strip()


def _band_ticks(bsobj):
    dists = bsobj.distances
    conns = list(bsobj.path_connections)
    labels = list(bsobj.labels)
    positions = [float(dists[0][0])] + [float(seg[-1]) for seg in dists]
    n = len(conns)
    out, li = [_clean_label(labels[0])], 1
    try:
        for s in range(n):
            if s == n - 1:
                out.append(_clean_label(labels[li])); li += 1
            elif conns[s]:
                out.append(_clean_label(labels[li])); li += 1
            else:
                out.append(_clean_label(labels[li]) + "|" + _clean_label(labels[li + 1])); li += 2
    except IndexError:
        return positions, [""] * len(positions)
    if len(out) != len(positions):
        return positions, [""] * len(positions)
    return positions, out


def _phonopy_primitive_to_pmg(phonon):
    from pymatgen.core import Structure as _PmgStructure
    pcell = phonon.primitive
    return _PmgStructure(np.array(pcell.cell), list(pcell.symbols),
                         np.array(pcell.scaled_positions), coords_are_cartesian=False)


def _resolve_band_path(phonon, band_yaml, npoints=101, symprec=1e-3):
    try:
        phonon.auto_band_structure(plot=False, write_yaml=True, filename=str(band_yaml))
        logging.info("[q路径] 使用 seekpath 自动路径")
        return
    except Exception as e:
        logging.info("[q路径] seekpath 不可用 (%s)，改用 pymatgen 高对称路径", e)
    from phonopy.phonon.band_structure import get_band_qpoints_and_path_connections
    try:
        from pymatgen.symmetry.bandstructure import HighSymmKpath
    except Exception:
        from pymatgen.symmetry.kpath import HighSymmKpath
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    struct = _phonopy_primitive_to_pmg(phonon)
    sga = SpacegroupAnalyzer(struct, symprec=symprec)
    logging.info("[q路径] 晶系=%s, 空间群=%s (No.%d)", sga.get_crystal_system(),
                 sga.get_space_group_symbol(), sga.get_space_group_number())
    kp = HighSymmKpath(struct, path_type="setyawan_curtarolo")
    kpts, branches = kp.kpath["kpoints"], kp.kpath["path"]
    band_paths, labels = [], []
    for br in branches:
        band_paths.append([np.asarray(kpts[l], dtype=float) for l in br])
        labels.extend(br)
    qpoints, conn = get_band_qpoints_and_path_connections(band_paths, npoints=npoints)
    phonon.run_band_structure(qpoints, path_connections=conn, labels=labels, with_eigenvectors=False)
    phonon.write_yaml_band_structure(filename=str(band_yaml))
    logging.info("[q路径] 使用 pymatgen Setyawan-Curtarolo 路径")


def _save_band_png_matplotlib(seg_d, seg_f, tick_pos, tick_lab, png_path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9.0, 5.6))
    for d, f in zip(seg_d, seg_f):
        for b in range(f.shape[1]):
            ax.plot(d, f[:, b], color="#1f5fbf", lw=1.2)
    ax.axhline(0, color="black", lw=0.8, ls="--")
    for xp in tick_pos[1:-1]:
        ax.axvline(xp, color="0.6", lw=0.8)
    ax.set_xticks(tick_pos); ax.set_xticklabels(tick_lab)
    ax.set_xlim(tick_pos[0], tick_pos[-1])
    ax.set_ylabel("Frequency (THz)"); ax.set_title(title)
    ax.grid(axis="y", color="0.92", lw=0.8)
    fig.tight_layout(); fig.savefig(str(png_path), dpi=150); plt.close(fig)


def compute_band_and_check(phonon, band_dir, C, nac_vasprun=None):
    import plotly.graph_objects as go
    imag_thr = C["imag_thr"]
    # nac_applied：NAC 是否真正成功读取并施加（默认关；仅 nac_vasprun 存在且读取
    #   成功时为 True）。图题/BORN 文件据此产出，避免读取失败仍误标 "(with NAC)"。
    nac_applied = False
    nac_data = None
    if nac_vasprun:
        try:
            nac_data = extract_nac_params(
                nac_vasprun, phonon, C["nac_factor"],
                symprec=C.get("symprec_std", 1e-5))
            phonon.nac_params = nac_data     # 校验已过,这里不会再抛
            nac_applied = True
            logging.info("已启用 NAC:含 Γ 点 LO-TO 劈裂")
        except Exception as e:
            logging.warning("NAC 提取失败,退回无 NAC 出谱:%s", e)
            # ★兜底:清坏 nac_params 并强制重建 plain 动力学矩阵。
            #   赋 None 会走 setter 的 else 分支重建无 NAC 的 DM,
            #   否则失败的赋值已把 _dynamical_matrix 置 None,后续出谱必报
            #   'Dynamical matrix has not yet built.'
            try:
                phonon.nac_params = None
            except Exception as e2:
                logging.warning("重建无 NAC 动力学矩阵失败:%s", e2)
    logging.info("力常数 shape = %s", phonon.force_constants.shape)
    band_yaml = band_dir / "band-dft-cpu.yaml"
    _resolve_band_path(phonon, band_yaml, npoints=C["band_npoints"], symprec=C["symprec_path"])

    bsobj = phonon.band_structure
    if bsobj is None:
        raise RuntimeError("phonon.band_structure 仍为 None：_resolve_band_path 未成功运行 band-dft-cpu")
    seg_d = [np.array(x) for x in bsobj.distances]
    seg_f = [np.array(x) for x in bsobj.frequencies]   # ← 紧接 seg_d,原来的第 990 行
    fmin = float(min(f.min() for f in seg_f))
    fmax = float(max(f.max() for f in seg_f))
    logging.info("频率范围: %.3f ~ %.3f THz", fmin, fmax)
    tick_pos, tick_lab = _band_ticks(bsobj)

    fig = go.Figure()
    for d, f in zip(seg_d, seg_f):
        for b in range(f.shape[1]):
            fig.add_trace(go.Scatter(x=d, y=f[:, b], mode="lines",
                          line=dict(color="#1f5fbf", width=1.3),
                          hovertemplate="q=%{x:.3f}<br>ν=%{y:.3f} THz<extra></extra>",
                          showlegend=False))
    fig.add_hline(y=0, line=dict(color="black", width=0.8, dash="dash"))
    for xp in tick_pos[1:-1]:
        fig.add_vline(x=xp, line=dict(color="rgba(0,0,0,0.35)", width=0.8))
    fig.update_layout(
        title="Phonon dispersion" + (" (with NAC)" if nac_applied else ""),
        xaxis=dict(tickmode="array", tickvals=tick_pos, ticktext=tick_lab,
                   range=[tick_pos[0], tick_pos[-1]], showgrid=False, ticks="outside"),
        yaxis=dict(title="Frequency (THz)", zeroline=False, showgrid=True, gridcolor="rgba(0,0,0,0.08)"),
        plot_bgcolor="white", paper_bgcolor="white",
        font=dict(family="DejaVu Sans, Arial", size=14),
        width=900, height=560, margin=dict(l=70, r=30, t=50, b=50))

    title = "Phonon dispersion" + (" (with NAC)" if nac_applied else "")

    # 交互式 HTML（附带产物）
    html_path = band_dir / "phonon_band.html"
    fig.write_html(str(html_path), include_plotlyjs="cdn")
    logging.info("交互式声子谱已保存: %s", html_path)

    result = {"phonon_band": str(html_path), "band_html": str(html_path),
              "nac_applied": nac_applied}

    # ── 声子色散谱 PNG（matplotlib 为主，稳定、不依赖 kaleido）────────────────
    png_path = band_dir / "phonon_band.png"
    png_done = False
    try:
        _save_band_png_matplotlib(seg_d, seg_f, tick_pos, tick_lab, png_path, title)
        png_done = True
    except Exception as e:
        logging.info("matplotlib PNG 失败（%s），改用 plotly/kaleido 兜底", e)
        try:
            fig.write_image(str(png_path), scale=2)
            png_done = True
        except Exception as e2:
            logging.warning("plotly PNG 兜底也失败：%s", e2)
    if png_done:
        result["phonon_band_png"] = str(png_path)
        logging.info("静态声子谱 PNG 已保存: %s", png_path)

    # ── 声子谱绘图数据（band-dft-cpu.yaml，phonopy 标准 yaml，可复现色散谱）───────────
    if band_yaml.is_file():
        result["phonon_band_data"] = str(band_yaml)
        logging.info("声子谱绘图数据(yaml)已保存: %s", band_yaml)

    # ── 介电常数 / Born（若 NAC 成功施加）→ phonopy BORN 文件（含 ε∞ + Born）──
    if nac_applied and nac_data is not None:
        try:
            from phonopy.file_IO import write_BORN
            born_path = band_dir / "BORN"
            write_BORN(phonon.primitive, nac_data["born"], nac_data["dielectric"],
                       filename=str(born_path))
            result["dielectric"] = str(born_path)
            result["born_file"] = str(born_path)
            logging.info("介电常数/Born(BORN 文件)已保存: %s", born_path)
        except Exception as e:
            logging.warning("BORN 文件导出失败: %s", e)

    has_imaginary = fmin < -imag_thr
    if has_imaginary:
        logging.warning("存在显著虚频（最低 %.3f THz，阈值 -%.1f）", fmin, imag_thr)
    else:
        logging.info("无显著虚频（最低 %.3f THz），结构稳定。", fmin)
    result.update({"stable": (not has_imaginary), "fmin": fmin, "fmax": fmax})
    return result


# ============================================================================
# 主入口
# ============================================================================
# ── 输入参数 (config) ──────────────────────────────────────────────────────
# 字段                类型    必填   取值范围                       值案例                              描述
# inputfile           URL     是     合法 HTTP URL                  "http://.../Si.zip"                 晶体结构文件 ZIP 包（内含 CIF/POSCAR/CONTCAR；下载到 input/）
# method              ENUM    否     "random"|"findiff"             "random"(默认)                      力常数法：random=hiphive 随机位移 / findiff=phono3py 有限位移
# kappa_solver        ENUM    否     "shengbte"|"phono3py"          "shengbte"(默认)                    BTE 求解器；method=findiff 必须用 "phono3py"
# magnetic            bool    否     true|false                     false(默认)                         自旋极化总开关；true 时自动写入 ISPIN/LORBIT/MAGMOM
# nac                 bool    否     true|false                     false(默认)                         NAC 非解析项修正（极性/离子型材料 LO-TO 劈裂），多走一次 DFPT 取介电常数
# magmom              ANY     否     null|{元素:μB}|[逐原子]        null(默认按内置磁矩表)              初始磁矩配置；magnetic=true 时生效；逐原子列表对应标准化后 POSCAR-prim0 顺序
# supercell           LIST    否     null|[a,b,c] 正整数            null(默认弛豫后自动计算)            显式扩胞倍数；给定即直接采用，跳过自动推荐；null=弛豫后按下三项自动算
# min_sc_length       float   否     >0 浮点(Å)                     7.0(默认)                           超胞最短边长下界(Å)；越大越收敛、DFT 越贵
# min_sc_diameter     float   否     >0 浮点(Å)                     6.0(默认)                           超胞内切球直径下界(Å)；约束 fc2/fc3 截断
# max_atoms           int     否     >0 整数                        600(默认)                           超胞原子数上限；超过则反向收缩 reps，可能让上面两项失约束并 warning
# n_nodes             int     否     >0 整数                        1(默认)                             HPC 申请节点数
# tasks_per_node      int     否     >0 整数                        64(默认)                            HPC 每节点 MPI 进程数（SLURM_NTASKS）
# mem                 str     否     "<N>GB"                        "64GB"(默认)                        HPC 任务内存
# 其余键（如 cmd / parentId / shengbte_exe / 各步位移幅度 / 截断 / 网格 / INCAR 模板…）
# 全部固化在脚本顶部 DEFAULTS / ADVANCED_DEFAULTS，由标准镜像 + 控制平面提供，不开放覆盖。
# ── 输出字段 (return dict) ─────────────────────────────────────────────────
# ── 常规完成返回 ──
# 字段              类型     描述
# method            str      实际使用的力常数法（"random"/"findiff"）
# kappa_solver      str      实际使用的 BTE 求解器
# magnetic          bool     本次是否走自旋极化
# nac               bool     本次请求是否做 NAC 修正
# nac_applied       bool     NAC 是否真正成功读取并施加（默认关；读取失败会退回无 NAC）
# relaxed           bool     本次是否完成结构优化（流程固化为 true）
# stable            bool     声子谱无显著虚频（|imag| < imag_thr）
# fmin / fmax       float    声子频率范围 (THz)
# phonon_band       str      交互式声子谱 HTML 路径
# phonon_band_png   str      声子色散谱 PNG 路径（phonon_band.png）
# phonon_band_data  str      声子谱绘图数据 yaml 路径（band-dft-cpu.yaml）
# force_constants_fc2 str    二阶力常数 fc2.hdf5 路径
# force_constants_fc3 str    三阶力常数 fc3.hdf5 路径（仅 kappa 时）
# dielectric        str      介电常数/Born 的 BORN 文件路径（仅 nac_applied 时）
# kappa_result      str      热导率结果 txt 路径（kappa_result.txt，仅 stable 时）
# kappa_fig         str      κ-T 曲线 PNG 路径（kappa_fig.png，同上）
# error_step / error_msg     出错时返回失败步骤名 + 错误描述
# ─────────────────────────────────────────────────────────────────────────
def run(config: Dict[str, Any]) -> dict:
    # 入参 whitelist：只让 _ALLOWED_OVERRIDES 里的键覆盖固化默认值；
    # 名单外的键一律忽略（含 cmd/parentId/shengbte_exe 等流程固化项），
    # 防止 config 误改标准 job 的流程定义。
    overrides = {k: v for k, v in (config or {}).items() if k in _ALLOWED_OVERRIDES}
    ignored = sorted(set((config or {}).keys()) - _ALLOWED_OVERRIDES)
    if ignored:
        logging.warning("[config] 以下键不在白名单，忽略不覆盖流程参数: %s", ignored)
    C = {**DEFAULTS, **ADVANCED_DEFAULTS, **overrides}
    step = "初始化"
    try:
        init_dirs()
        METHOD = C["method"]
        if METHOD not in VALID_METHODS:
            raise ValueError(f"method 仅支持 {VALID_METHODS}，收到: {METHOD!r}")
        DO_RELAX, DO_NAC, DO_MAG = bool(C["relax"]), bool(C["nac"]), bool(C["magnetic"])
        DO_KAPPA = bool(C["kappa"])
        # 快速失败：findiff 的 compact fc3 没有可靠 ShengBTE 导出口；提前拦，别白跑整批 DFT
        if DO_KAPPA and METHOD == "findiff" and C["kappa_solver"] == "shengbte":
            raise ValueError(
                "findiff + kappa 只支持 kappa_solver='phono3py'（phono3py 的 compact fc3 "
                "无可靠 ShengBTE 导出口）。请把 kappa_solver 改成 'phono3py'，"
                "或对 ShengBTE 改用 method='random'。")
        if C["kappa_solver"] not in ("shengbte", "phono3py"):
            raise ValueError(f"kappa_solver 仅支持 'shengbte'/'phono3py'，收到 {C['kappa_solver']!r}")
        logging.info("方法=%s | 结构优化=%s | NAC=%s | 磁性=%s",
                     METHOD, DO_RELAX, DO_NAC, DO_MAG)
        logging.info("热导率=%s | 求解器=%s | 工作晶胞=%s | encut_scale=%.2f | min_sc_length=%.1f Å",
                     DO_KAPPA, C["kappa_solver"], C["cell_type"],
                     float(C["encut_scale"]), float(C["min_sc_length"]))
        logging.info("HPC 资源: n_nodes=%s, tasks_per_node=%s, mem=%s",
                     C.get("n_nodes"), C.get("tasks_per_node"), C.get("mem"))

        # ── 步骤 1：读取 + 标准化原胞 ───────────────────────────────────────
        step = "步骤1 读取结构 + 标准化原胞"
        logging.info("=" * 60); logging.info("[%s]", step)
        raw = read_input_structure(C["input_dir"])
        prim = standardize_cell(raw, cell_type=C["cell_type"], symprec=C["symprec_std"])
        SYSTEM = prim.get_chemical_formula("metal")
        logging.info("体系 = %s, 原胞原子数 = %d", SYSTEM, len(prim))

        # 原胞 POTCAR / 基准 ENCUT
        prim_poscar = SUBDIRS["relax"] / "POSCAR-prim0"
        SUBDIRS["relax"].mkdir(parents=True, exist_ok=True)
        ase_write(str(prim_poscar), prim, format="vasp", direct=True, sort=False)
        prim_potcar = SUBDIRS["relax"] / "POTCAR-prim"
        generate_potcar(prim_poscar, prim_potcar)
        if C["encut"]:
            base_encut = float(C["encut"])
            logging.info("基准 ENCUT = %.0f eV（用户强制 encut，忽略 encut_scale）", base_encut)
        else:
            enmax = encut_from_potcar(prim_potcar)
            base_encut = enmax * C["encut_scale"]
            logging.info("基准 ENCUT = max(ENMAX) %.0f × %.2f = %.0f eV",
                         enmax, C["encut_scale"], base_encut)

        # 基础 INCAR 模板（继承链起点）
        base_incar = load_base_incar(C, formula=SYSTEM, encut=base_encut)

        # 磁矩：原胞初始磁矩（保留初猜副本，供后续「塌缩守卫」比对）
        prim_moments = build_primitive_moments(prim, C["magmom"], C["magmom_default"]) if DO_MAG else None
        prim_moments_init = list(prim_moments) if prim_moments else None

        # ── 步骤 2：结构优化 ISIF=3 ─────────────────────────────────────────
        step = "步骤2 结构优化 (ISIF=3)"
        logging.info("=" * 60); logging.info("[%s]", step)
        if DO_RELAX:
            relax_incar = finalize_incar(
                C, base_incar, C["relax_incar_overrides"],
                encut=base_encut, system="relax",
                moments=prim_moments, symbols=prim.get_chemical_symbols())
            prim = run_vasp_relax(prim, SUBDIRS["relax"], relax_incar, C)
            # 磁矩继承①：relax 收敛磁矩 → static 初猜。弛豫几何对应弛豫收敛到的那个
            #   磁性解；static 若从表初猜重启可能落到另一个解，导致几何-磁性不自洽。
            if DO_MAG and C["magmom_inherit"]:
                inh = read_magmoms_from_outcar(SUBDIRS["relax"] / "OUTCAR",
                                               n_expected=len(prim))
                if inh is not None:
                    prim_moments = inh
                    logging.info("[磁矩] static 将继承弛豫收敛磁矩作初猜")
                else:
                    logging.warning("[磁矩] 未能从弛豫 OUTCAR 继承，static 沿用初猜表")
        else:
            logging.info("跳过结构优化（relax=False）")

        # ── 步骤 2.5：金属判据 → 金属自动跳过 NAC ───────────────────────────
        #   NAC 修正的是极性【绝缘体/半导体】Γ 点的 LO-TO 劈裂。金属的自由载流子
        #   会屏蔽宏观纵向电场：Born 有效电荷 / LO-TO 劈裂无物理意义，且 DFPT 介电
        #   (LEPSILON) 对金属发散/不适定。故检测到金属即跳过 NAC 并提醒用户。
        #   relax=True 时已有弛豫 vasprun，可在跑 LEPSILON 前就判金属 → 零浪费。
        metal_nac_note = None
        if DO_NAC and C["skip_nac_if_metal"] and DO_RELAX:
            is_metal, gap = detect_is_metal(SUBDIRS["relax"] / "vasprun.xml",
                                            gap_thr=C["metal_gap_thr"])
            if is_metal:
                DO_NAC = False; C["nac"] = False   # 同步 C["nac"]，让步骤10 热导率端也跳过 NAC
                metal_nac_note = (f"检测到金属（band-dft-cpu gap ≈ {gap:.3f} eV < "
                                  f"{C['metal_gap_thr']:.3f} eV），已自动跳过 NAC："
                                  "金属自由载流子屏蔽宏观纵场，无 LO-TO 劈裂，DFPT 介电亦不适定。")
                logging.warning("★" * 60)
                logging.warning("[NAC 跳过] %s", metal_nac_note)
                logging.warning("★" * 60)
            elif is_metal is None:
                logging.warning("[金属判据] 未能从弛豫 vasprun 判定金属/绝缘体，"
                                "按用户设置继续做 NAC（若 ε∞ 异常会在出谱时自动退回无 NAC）")

        # ── 步骤 3：静态计算（继承模板 + 覆盖；NAC 时叠加 LEPSILON）─────────
        step = "步骤3 静态计算" + ("（NAC/LEPSILON）" if DO_NAC else "")
        logging.info("=" * 60); logging.info("[%s]", step)
        static_extra = C["nac_incar_overrides"] if DO_NAC else None
        static_incar = finalize_incar(
            C, base_incar, C["static_incar_overrides"],
            encut=base_encut, system=("nac_lepsilon" if DO_NAC else "static"),
            moments=prim_moments, symbols=prim.get_chemical_symbols(), extra=static_extra)
        static_vasprun = run_vasp_static(prim, SUBDIRS["static"], static_incar, C, nac=DO_NAC)

        # relax=False 时无弛豫 vasprun，无法在跑 LEPSILON 前判金属；此处用 NAC 静态
        #   自身的 vasprun 兜底判：若实为金属，丢弃 NAC 并提醒（该 DFPT 介电已是无谓
        #   计算，建议下次设 relax=True 以提前判金属、免跑无谓 LEPSILON）。
        if DO_NAC and C["skip_nac_if_metal"] and not DO_RELAX:
            is_metal, gap = detect_is_metal(static_vasprun, gap_thr=C["metal_gap_thr"])
            if is_metal:
                DO_NAC = False; C["nac"] = False
                metal_nac_note = (f"检测到金属（band-dft-cpu gap ≈ {gap:.3f} eV），已跳过 NAC。"
                                  "本次 relax=False 无法在 LEPSILON 前预判，该 DFPT 介电为"
                                  "无谓计算；建议下次设 relax=True 以提前判金属。")
                logging.warning("★" * 60)
                logging.warning("[NAC 跳过] %s", metal_nac_note)
                logging.warning("★" * 60)

        nac_vasprun = static_vasprun if DO_NAC else None
        # 磁矩继承②：static 收敛磁矩（球投影校正后）→ 超胞取力初猜
        if DO_MAG and C["magmom_inherit"]:
            inh = read_magmoms_from_outcar(SUBDIRS["static"] / "OUTCAR",
                                           n_expected=len(prim))
            if inh is not None:
                # 塌缩守卫：初猜有磁而收敛+校正后近零 → 可能是投影低估/初猜不当
                #   导致 SCF 塌到 NM 假解；继承会把塌缩传播给全部取力帧，先提醒。
                if (max(abs(m) for m in prim_moments_init) >= 0.5
                        and max(abs(m) for m in inh) < MAG_COLLAPSE_THR):
                    logging.warning("★" * 60)
                    logging.warning("[磁矩] 疑似磁性塌缩：初猜 max|m|=%.2f μB，静态收敛后 "
                                    "max|m|=%.2f < %.2f μB。若该体系应有磁性，请核查静态计算"
                                    "（可尝试增大 magmom 初猜 / 检查 U 值 / 收敛设置）；"
                                    "全部超胞取力将以近零磁矩起步。",
                                    max(abs(m) for m in prim_moments_init),
                                    max(abs(m) for m in inh), MAG_COLLAPSE_THR)
                    logging.warning("★" * 60)
                prim_moments = inh
                logging.info("[磁矩] 超胞将继承原胞静态收敛磁矩（已校正）")
            else:
                logging.warning("[磁矩] 未能从静态 OUTCAR 继承，超胞沿用当前初猜: %s",
                                magmom_to_incar_string(prim_moments))

        # ── 步骤 4：扩胞 + phonopy 超胞（全链同序，修复 pymatgen 错位 bug）──
        #   超胞在此根据当前 prim 确定：DO_RELAX 时 prim 已是弛豫后结构 → 按弛豫后扩胞；
        #   未弛豫则按初始结构扩胞。显式 supercell 直接采用（尺寸只 warning）；否则按
        #   min_sc_length/min_sc_diameter/max_atoms 计算到满足要求。
        step = "步骤4 扩胞 + 超胞"
        logging.info("=" * 60); logging.info("[%s]", step)
        _basis = "弛豫后结构" if DO_RELAX else "初始结构（未弛豫）"
        if C["supercell"] is not None:
            SUPERCELL, _ = validate_user_supercell(
                prim, C["supercell"], C["min_sc_length"], C["min_sc_diameter"], C["max_atoms"])
            logging.info("超胞倍数 SUPERCELL = %s（用户指定，基于%s核查）", SUPERCELL, _basis)
        else:
            SUPERCELL, _ = compute_supercell_reps(prim, C["min_sc_length"], C["min_sc_diameter"], C["max_atoms"])
            logging.info("超胞倍数 SUPERCELL = %s（按%s自动计算）", SUPERCELL, _basis)
        # ── 超胞 DFT 前：统一打印最终扩胞体量 + 近上限预警（不拦截）──
        _sz = supercell_size_summary(prim, SUPERCELL)
        logging.info("[扩胞体量] reps=%s | 超胞原子数=%d | 边长=%s Å | 内切球=%.2f Å | "
                     "依据 min_sc_length=%.1f, min_sc_diameter=%.1f, max_atoms=%d",
                     tuple(int(x) for x in SUPERCELL), _sz["n_atoms"], _sz["edges"],
                     _sz["insphere"], C["min_sc_length"], C["min_sc_diameter"], C["max_atoms"])
        if _sz["n_atoms"] > SC_NEAR_CAP_FRAC * C["max_atoms"]:
            logging.warning("[扩胞体量] 超胞 %d 原子已达 max_atoms=%d 的 %.0f%%，后续超胞单点 "
                            "DFT 开销较大；如需压体量可下调 min_sc_length/min_sc_diameter 或"
                            "显式传更小的 supercell 重跑。",
                            _sz["n_atoms"], C["max_atoms"],
                            100.0 * _sz["n_atoms"] / C["max_atoms"])
        logging.info("原胞 %d -> 超胞 %d 原子", len(prim), len(prim) * int(np.prod(SUPERCELL)))
        sc_poscar = SUBDIRS["supercell"] / "POSCAR"
        supercell_atoms = make_supercell_phonopy(prim, SUPERCELL, sc_poscar)
        N_SC = len(supercell_atoms)
        stale_rattles = list(SUBDIRS["rattle"].glob("POSCAR-[0-9]*"))
        for stale in stale_rattles:
            stale.unlink()
        if stale_rattles:
            logging.info("已清理上轮 rattle 微扰文件: %d 个", len(stale_rattles))

        # ── 步骤 5+6：位移配置 + 微扰超胞（按方法分叉）──────────────────────
        rattle_dir = SUBDIRS["rattle"]
        if METHOD == "random":
            step = "步骤5 ALM 自由参数(2阶[+3阶]) + N_STRUCT"
            logging.info("=" * 60); logging.info("[%s]", step)
            #   kappa=True：算 2+3 阶（三阶 cutoff=fc3_cutoff），N_STRUCT 覆盖 fc3，位移幅度 0.03
            #   kappa=False：只算 2 阶，只够 fc2 出声子谱，位移幅度 0.01
            if DO_KAPPA:
                ORDERS, CUTOFFS = [2, 3], [None, C["fc3_cutoff"]]
                disp_amp = C["hiphive_disp"]
            else:
                ORDERS, CUTOFFS = [2], [None]
                disp_amp = C["hiphive_disp_band"]
            write_alm_suggest_input(supercell_atoms, ORDERS, CUTOFFS,
                                    SUBDIRS["alm"] / "alm.in", tolerance=C["alm_tolerance"])
            alm_log = run_alm(SUBDIRS["alm"])
            NFREE = parse_alm_nfree(alm_log, ORDERS)
            N_STRUCT = estimate_n_struct(NFREE, N_SC, C["oversample"], C["n_struct_min"])

            step = "步骤6 MC-rattle 微扰构型（随机位移）"
            logging.info("=" * 60); logging.info("[%s] 位移幅度=%.3f Å (kappa=%s)", step, disp_amp, DO_KAPPA)
            rattled_list, _rstd, _rms = generate_calibrated_mc_rattle(
                supercell_atoms, N_STRUCT, target_rms=disp_amp,
                dmin_scale=C["mc_dmin_scale"], n_iter=C["mc_n_iter"], seed=C["seed"],
                max_cal=C["mc_cal_max"], tol=C["mc_cal_tol"], n_probe_cap=C["mc_cal_probe"])
            if not rattled_list:
                raise RuntimeError("MC-rattle 未生成任何微扰结构")
            disp_files = []
            for i, rattled in enumerate(rattled_list, start=1):
                out_path = rattle_dir / f"POSCAR-{i:05d}"
                ase_write(str(out_path), rattled, format="vasp", direct=True, sort=False)
                disp_files.append(out_path)
            eq_supercell_atoms = supercell_atoms
            logging.info("MC-rattle 完成，%d 个微扰构型", len(disp_files))
        elif not DO_KAPPA:  # findiff，仅声子谱：phonopy 0.01 二阶有限位移
            step = "步骤5+6 phonopy 有限位移生成（0.01，仅 fc2/声子谱）"
            logging.info("=" * 60); logging.info("[%s]", step)
            from phonopy import load as phonopy_load
            from phonopy.interface.vasp import write_vasp
            unitcell_path = SUBDIRS["band-dft-cpu"] / "POSCAR-unitcell"
            ase_write(str(unitcell_path), prim, format="vasp", sort=False)
            phonon = phonopy_load(unitcell_filename=str(unitcell_path),
                                  supercell_matrix=np.diag(SUPERCELL),
                                  primitive_matrix="auto", produce_fc=False, log_level=0)
            phonon.generate_displacements(distance=C["findiff_disp"])
            scs, perfect = phonon.supercells_with_displacements, phonon.supercell
            logging.info("对称约化位移数 = %d (幅度 %.3f Å), 超胞原子数 = %d",
                         len(scs), C["findiff_disp"], len(perfect))
            disp_yaml = SUBDIRS["band-dft-cpu"] / "phonopy_disp.yaml"
            phonon.save(str(disp_yaml))
            sposcar = rattle_dir / "SPOSCAR"
            write_vasp(str(sposcar), perfect)
            eq_supercell_atoms = ase_read(str(sposcar), format="vasp")
            disp_files = []
            for i, sc in enumerate(scs, start=1):
                out_path = rattle_dir / f"POSCAR-{i:05d}"
                write_vasp(str(out_path), sc)
                disp_files.append(out_path)
            logging.info("有限位移超胞写出 %d 个", len(disp_files))
        else:  # findiff + kappa：phono3py 0.03 三阶位移「一批」，后续 produce fc2+fc3
            step = "步骤5+6 phono3py 三阶有限位移生成（0.03，一批共用 fc2+fc3）"
            logging.info("=" * 60); logging.info("[%s]", step)
            from phono3py import Phono3py
            from phonopy.structure.atoms import PhonopyAtoms
            from phonopy.interface.vasp import write_vasp
            #   标准 phono3py 主流：一批 0.03 位移 → 同时产 fc2 与 fc3；声子谱用这个 fc2，
            #   κ 用同批的 fc2+fc3（与 random 的 rattle-0.03 单批拟合完全对称）。
            unitcell_path = SUBDIRS["band-dft-cpu"] / "POSCAR-unitcell"
            ase_write(str(unitcell_path), prim, format="vasp", sort=False)
            unit = PhonopyAtoms(symbols=prim.get_chemical_symbols(),
                                scaled_positions=prim.get_scaled_positions(),
                                cell=prim.cell[:])
            ph3 = Phono3py(unit, supercell_matrix=np.diag(SUPERCELL), primitive_matrix="auto")
            cpd = C["findiff_fc3_cutoff_pair"]
            ph3.generate_displacements(distance=float(C["findiff_fc3_disp"]),
                                       cutoff_pair_distance=cpd)
            # phono3py 位移定义存到 7_band，步骤9 reload 同一批产 fc2+fc3
            disp_yaml = SUBDIRS["band-dft-cpu"] / "phono3py_disp.yaml"
            ph3.save(str(disp_yaml))
            perfect = ph3.supercell
            sposcar = rattle_dir / "SPOSCAR"
            write_vasp(str(sposcar), perfect)
            eq_supercell_atoms = ase_read(str(sposcar), format="vasp")

            all_scs = ph3.supercells_with_displacements        # 含 None（被 cutoff_pair 砍掉）
            todo = [(i, s) for i, s in enumerate(all_scs) if s is not None]  # (dataset 下标, 超胞)
            n_total, n_todo = len(all_scs), len(todo)
            logging.info("[findiff/kappa] phono3py 三阶位移：dataset 共 %d，需算 %d"
                         "（cutoff_pair=%s 砍掉 %d），幅度 %.3f Å —— 纯有限差分，不走 ALM",
                         n_total, n_todo, cpd, n_total - n_todo, C["findiff_fc3_disp"])
            if cpd is None and n_todo > 300:
                logging.warning("三阶位移超胞数 %d 偏多（全对称集 + 大超胞）；"
                                "建议设 findiff_fc3_cutoff_pair=4~6 Å 削减 DFT 量", n_todo)
            #   目录名用「dataset 下标(从 1 起)」，被 cutoff 砍掉的槽位天然缺号；
            #   步骤8 据实际目录号收集 used_indices，步骤9 按完整 dataset 长度回填。
            disp_files = []
            for idx, sc in todo:
                out_path = rattle_dir / f"POSCAR-{idx + 1:05d}"
                write_vasp(str(out_path), sc)
                disp_files.append(out_path)
            logging.info("三阶有限位移超胞写出 %d 个（共 %d，含 None 缺号）", len(disp_files), n_total)

        # ── 步骤 7：超胞 DFT 单点取力（继承静态参数 + 覆盖；磁矩超胞映射）──
        step = "步骤7 DFT 单点取力"
        logging.info("=" * 60); logging.info("[%s]", step)
        dft_dir = SUBDIRS["dft"]
        stale_dft = [p for p in dft_dir.glob("POSCAR-[0-9]*") if p.is_dir()]
        for old in stale_dft:
            shutil.rmtree(old)
        if stale_dft:
            logging.info("已清理上轮 5_dft 子目录: %d 个", len(stale_dft))

        ref_poscar = dft_dir / "POSCAR-ref"
        ase_write(str(ref_poscar), eq_supercell_atoms, format="vasp", direct=True, sort=False)
        potcar_path = dft_dir / "POTCAR"
        generate_potcar(ref_poscar, potcar_path)
        if C["encut"]:
            encut_sc = float(C["encut"])
        else:
            encut_sc = encut_from_potcar(potcar_path) * C["encut_scale"]
        logging.info("超胞力计算 ENCUT = %.0f（与 relax/static 一致）", encut_sc)

        # 超胞磁矩：按 eq_supercell_atoms 真实原子顺序几何映射（原子数已变）
        sc_moments = None
        if DO_MAG:
            sc_moments = expand_magmom_to_supercell(prim, prim_moments, eq_supercell_atoms, SUPERCELL)
            logging.info("[磁矩] 已按几何映射到超胞，逐原子磁矩 = %d 个", len(sc_moments))

        # 超胞取力 INCAR：
        #   开关 use_supercell_incar_template=True → 直接读独立静态模板（绕过继承链）；
        #   否则 → 继承链：静态(base+static_overrides) + dft_overrides。
        #   两条路径都仍由 finalize_incar 注入 ENCUT(+超胞 MAGMOM)，其余按字面值。
        if C["use_supercell_incar_template"]:
            logging.info("超胞取力：启用独立静态模板 %s（绕过继承链）", C["supercell_incar_template"])
            sc_base = load_base_incar(
                C, formula=SYSTEM, encut=encut_sc,
                tpl_path=C["supercell_incar_template"],
                fallback=DEFAULT_SUPERCELL_INCAR_TEMPLATE)
            dft_incar_text = finalize_incar(
                C, sc_base, {},                      # 模板即最终参数，无额外覆盖
                encut=encut_sc, system=f"{SYSTEM}_force",
                moments=sc_moments, symbols=eq_supercell_atoms.get_chemical_symbols())
        else:
            # DFT INCAR = 静态(base+static_overrides) 继承 + dft_overrides 覆盖
            logging.info("超胞取力：走继承链 base + static_overrides + dft_overrides")
            dft_base = merge_incar(base_incar, C["static_incar_overrides"])
            dft_incar_text = finalize_incar(
                C, dft_base, C["dft_incar_overrides"],
                encut=encut_sc, system=f"{SYSTEM}_force",
                moments=sc_moments, symbols=eq_supercell_atoms.get_chemical_symbols())

        disp_dirs = []
        for f in disp_files:
            tgt = dft_dir / f.name
            tgt.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(f), tgt / "POSCAR")
            (tgt / "INCAR").write_text(dft_incar_text, encoding="utf-8")
            shutil.copy2(potcar_path, tgt / "POTCAR")
            disp_dirs.append(tgt)

        eq_dir = dft_dir / "POSCAR-00000"
        eq_dir.mkdir(parents=True, exist_ok=True)
        ase_write(str(eq_dir / "POSCAR"), eq_supercell_atoms, format="vasp", direct=True, sort=False)
        (eq_dir / "INCAR").write_text(dft_incar_text, encoding="utf-8")
        shutil.copy2(potcar_path, eq_dir / "POTCAR")
        disp_dirs.append(eq_dir)
        logging.info("已分发 %d 个微扰目录 + 1 个平衡帧", len(disp_dirs) - 1)

        # 微扰目录互不依赖，全部并发提交、等所有任务结束（CountDownLatch 风格）。
        # ★submit_vasp_many 在「任一任务失败」时会等其余收尾后聚合抛错。这里必须捕获：
        #   否则单帧失败(如 ZHEGV/节点抖动)会直接冒泡到 run() 外层 except 终止整个流程，
        #   使下面的自动重算(dft_retry)与步骤8的容错跳过(min_success_ratio)全部沦为死代码——
        #   这正是"报错没跳过、流程中断"的根因。完成度不依赖提交返回码，而由 _dft_dir_complete
        #   逐目录核验(检查 OUTCAR 结束标志),比信任聚合返回更可靠;真·全失败会被平衡帧硬门槛
        #   与步骤8的成功帧下限兜住。
        try:
            hpc.submit_vasp_many([str(d) for d in disp_dirs], C)
            logging.info("所有 VASP 静态计算任务提交完成")
        except Exception as e:
            logging.warning("submit_vasp_many 报告有失败/未完成帧(先不中断,交由重算/跳过处理): %s", e)

        # ── 失败/未完成帧自动重算(平衡帧含在 disp_dirs 内,一并重投)──────────
        n_retry = int(C.get("dft_retry", 0))
        for attempt in range(1, n_retry + 1):
            bad = [d for d in disp_dirs if not _dft_dir_complete(d)]
            if not bad:
                logging.info("[重算] 全部 %d 帧均正常结束", len(disp_dirs))
                break
            names = ", ".join(p.name for p in bad[:12]) + (" ..." if len(bad) > 12 else "")
            logging.warning("[重算 %d/%d] %d 帧未正常结束,清旧输出后重投: %s",
                            attempt, n_retry, len(bad), names)
            for d in bad:                       # 清半成品输出,保留 INCAR/POSCAR/POTCAR
                for junk in ("OUTCAR", "OUTCAR.gz", "vasprun.xml", "OSZICAR"):
                    p = d / junk
                    if p.is_file():
                        p.unlink()
            try:
                hpc.submit_vasp_many([str(d) for d in bad], C)
            except Exception as e:                # 本轮重算仍有失败帧也不中断,交下一轮/核验处理
                logging.warning("[重算 %d/%d] 本轮仍报失败帧(继续核验): %s", attempt, n_retry, e)

        still_bad = [d.name for d in disp_dirs if not _dft_dir_complete(d)]
        if still_bad:
            logging.warning("[重算结束] 仍有 %d 帧未完成,后续按方法处理"
                            "(random 跳过 / findiff 报缺帧): %s",
                            len(still_bad),
                            ", ".join(still_bad[:12]) + (" ..." if len(still_bad) > 12 else ""))
        else:
            logging.info("[重算结束] 全部微扰帧 + 平衡帧均正常结束")

        # 🟥 平衡帧硬门槛:必须算完(力常数的零点参考),否则整批力都失去基准
        if not _dft_dir_complete(eq_dir):
            raise RuntimeError(
                f"平衡帧 {eq_dir.name} 未正常结束;平衡帧必须成功(力常数零点参考)。"
                "请检查该目录 OUTCAR/报错后重跑,或增大 dft_retry。")

        # 🟥 检查点：确认 5_dft/POSCAR-*/ 下均有 OUTCAR 再继续

        # ── 步骤 8：收集力-位移数据集 ───────────────────────────────────────
        step = "步骤8 收集力-位移数据集"
        logging.info("=" * 60); logging.info("[%s]", step)
        from pymatgen.io.vasp import Poscar
        EQ_DIRNAME = "POSCAR-00000"
        SUBDIR_PATTERN = re.compile(r"^POSCAR-(\d+)$")

        def read_poscar_frac(path):
            try:
                return Poscar.from_file(str(path)).structure.frac_coords
            except Exception:
                return Structure.from_file(str(path)).frac_coords

        def read_outcar_forces(path):
            open_fn = gzip.open if str(path).endswith(".gz") else open
            in_block, current, last = False, [], []
            with open_fn(str(path), "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "POSITION" in line and "TOTAL-FORCE" in line:
                        in_block, current = True, []
                        continue
                    if in_block:
                        if "total drift" in line:
                            if current:
                                last = current
                            in_block = False
                            continue
                        parts = line.split()
                        if len(parts) == 6:
                            try:
                                current.append([float(parts[3]), float(parts[4]), float(parts[5])])
                            except ValueError:
                                in_block = False
            if not last:
                raise ValueError(f"未找到 POSITION/TOTAL-FORCE 块: {path}")
            return np.array(last)

        def extract_one(d, tag):
            poscar, outcar = d / "POSCAR", d / "OUTCAR"
            if not outcar.exists():
                outcar = d / "OUTCAR.gz"
            if not poscar.exists() or not outcar.exists():
                raise FileNotFoundError(f"{tag}: 缺 POSCAR 或 OUTCAR")
            frac, forces = read_poscar_frac(poscar), read_outcar_forces(outcar)
            if forces.shape != frac.shape:
                raise ValueError(f"{tag}: 力 {forces.shape} != 位置 {frac.shape}")
            logging.info("  %s  n=%3d  max|F|=%.4f", tag, frac.shape[0],
                         np.linalg.norm(forces, axis=1).max())
            return frac, forces

        matched = []
        for entry in dft_dir.iterdir():
            if entry.is_dir() and entry.name != EQ_DIRNAME:
                m = SUBDIR_PATTERN.match(entry.name)
                if m and int(m.group(1)) >= 1:
                    matched.append((int(m.group(1)), entry))
        matched.sort(key=lambda x: x[0])
        if not matched:
            raise RuntimeError("未找到 POSCAR-NNN (NNN>=1) 目录")
        indices = [i for i, _ in matched]
        calc_dirs = [d for _, d in matched]
        logging.info("扫描到 %d 个位移目录（编号 %05d ~ %05d）",
                     len(matched), indices[0], indices[-1])

        all_frac, all_forces, used_indices, skipped = [], [], [], []
        for i, d in zip(indices, calc_dirs):
            try:
                frac, forces = extract_one(d, f"POSCAR-{i:05d}")
            except Exception as e:
                logging.warning("跳过 POSCAR-%05d: %s", i, e)
                skipped.append(i); continue
            all_frac.append(frac); all_forces.append(forces); used_indices.append(i)
        if not all_frac:
            raise RuntimeError("没有任何位移结构成功提取力数据")
        if skipped:
            logging.warning("跳过 %d 个目录: %s", len(skipped), ", ".join(f"{x:05d}" for x in skipped))
        logging.info("成功提取 %d / %d 个位移结构", len(used_indices), len(indices))

        # random:成功帧太少则报错(findiff 的完整性由步骤9 严格校验,不在此判占比)
        if METHOD == "random":
            n_ok, n_all = len(used_indices), len(indices)
            ratio = n_ok / max(n_all, 1)
            min_ratio = float(C.get("min_success_ratio", 0.0))
            min_frames = int(C.get("min_success_frames", 0))
            if ratio < min_ratio or n_ok < min_frames:
                raise RuntimeError(
                    f"随机位移成功帧过少:{n_ok}/{n_all}(占比 {ratio:.0%},下限 {min_ratio:.0%}"
                    + (f",绝对下限 {min_frames}" if min_frames else "")
                    + ")。请检查失败目录后重算,或增大 dft_retry / 调低下限。")
            logging.info("[random] 成功帧 %d/%d(占比 %.0f%%)达标,跳过 %d 个失败帧继续",
                         n_ok, n_all, ratio * 100, len(indices) - n_ok)

        eq_dir_dft = dft_dir / EQ_DIRNAME
        if not eq_dir_dft.is_dir():
            raise RuntimeError(f"平衡帧目录 {EQ_DIRNAME} 不存在")
        eq_frac, eq_forces = extract_one(eq_dir_dft, EQ_DIRNAME + " (eq)")
        if eq_frac.shape != all_frac[0].shape:
            raise ValueError(f"平衡帧原子数 {eq_frac.shape} 与位移结构 {all_frac[0].shape} 不一致")
        all_frac.append(eq_frac); all_forces.append(eq_forces)
        logging.info("[自检] 平衡帧 max|F| = %.4f eV/A", np.linalg.norm(eq_forces, axis=1).max())

        dataset_disps = np.array(all_frac)
        dataset_forces = np.array(all_forces)
        np.save(dft_dir / "dataset_disps.npy", dataset_disps)
        np.save(dft_dir / "dataset_forces.npy", dataset_forces)
        np.save(dft_dir / "used_indices.npy", np.array(used_indices))
        logging.info("dataset_disps %s, dataset_forces %s（末帧=平衡帧）",
                     dataset_disps.shape, dataset_forces.shape)

        # ── 步骤 9：求 fc2[+fc3] → 声子谱 ───────────────────────────────────
        fc_paths_findiff = None
        if METHOD == "random":
            result, fcs_random = fit_fc2_hiphive(prim, supercell_atoms, SUPERCELL,
                                                 dft_dir, SUBDIRS, C, nac_vasprun)
        elif DO_KAPPA:
            # findiff + kappa：reload 0.03 单批 → 同时 produce fc2+fc3 → 谱用该 fc2
            result, fc_paths_findiff = fit_fc2fc3_findiff(
                prim, SUPERCELL, dft_dir, SUBDIRS, C, nac_vasprun)
            fcs_random = None
        else:
            # findiff 仅声子谱：phonopy 0.01 → fc2
            result = fit_fc2_findiff(SUPERCELL, dft_dir, SUBDIRS, used_indices, C, nac_vasprun)
            fcs_random = None
        result.update({"method": METHOD, "nac": DO_NAC, "relaxed": DO_RELAX, "magnetic": DO_MAG})
        if metal_nac_note:
            result["metal"] = True
            result["nac_skipped_reason"] = metal_nac_note

        # ====================================================================
        # 步骤 10：晶格热导率（声子谱无虚频时才计算）
        #   random ：复用步骤9 已拟合的 fcs(含 fc3)，不再二次拟合
        #   findiff：复用步骤9「同一批 0.03」produce 的 fc2+fc3，不再另跑 DFT
        # ====================================================================
        if C["kappa"]:
            if not result.get("stable", True):
                logging.warning("声子谱存在显著虚频，跳过热导率计算（结构动力学不稳定）")
                result["kappa_result"] = "skipped: 声子谱存在虚频，热导率无意义"
            else:
                step = "步骤10 晶格热导率"
                kappa_res = compute_kappa(
                    C, prim, supercell_atoms, SUPERCELL, SYSTEM, dft_dir, SUBDIRS,
                    METHOD, C, fcs_random=fcs_random, fc_paths_findiff=fc_paths_findiff)
                result.update(kappa_res)

        # ── 力常数产物路径（供结果展示）：2 阶总有；3 阶仅 kappa 时有 ──────────
        fc2_disp = SUBDIRS["band-dft-cpu"] / "fc2.hdf5"
        if fc2_disp.is_file():
            result["force_constants_fc2"] = str(fc2_disp)
        for cand in (SUBDIRS["band-dft-cpu"] / "fc3.hdf5",
                     SUBDIRS["fcs"] / "fc3.hdf5",
                     SUBDIRS["kappa"] / "fc3.hdf5"):
            if cand.is_file():
                result["force_constants_fc3"] = str(cand)
                break

        logging.info("=" * 60)
        logging.info("✅ 全流程完成 | stable=%s | fmin=%.3f THz | fmax=%.3f THz",
                     result.get("stable"), result.get("fmin", float("nan")),
                     result.get("fmax", float("nan")))
        return result

    except Exception as e:
        logging.error("❌ [%s] 出错: %s", step, e)
        logging.error("堆栈:\n%s", traceback.format_exc())
        return {"error_step": step, "error_msg": str(e)}


# ============================================================================
# 步骤 9：hiphive 随机位移法
# ============================================================================
def fit_fc2_hiphive(prim, supercell_atoms, SUPERCELL, dft_dir, SUBDIRS, C, nac_vasprun):
    """随机位移法：从 rattle 力数据集【一次拟合】fc2[+fc3]。
    kappa=True 时 cutoffs=[fc2,fc3] 同时拟合 fc2+fc3，fc2 用于声子谱、fc2+fc3 留给热导率；
    kappa=False 时只拟合 fc2 出声子谱。返回 (band_result, fcs)；fcs 供热导率直接复用，不再二次拟合。"""
    step = "步骤9 随机位移 拟合 fc2[+fc3] + 声子谱"
    logging.info("=" * 60); logging.info("[%s]", step)
    from hiphive import (ClusterSpace, StructureContainer,
                         ForceConstantPotential, enforce_rotational_sum_rules)
    from trainstation import Optimizer
    from phonopy import load as phonopy_load

    def _max_safe_cutoff(atoms, margin):
        cell = np.asarray(atoms.cell.array if hasattr(atoms.cell, "array") else atoms.cell)
        V = abs(np.linalg.det(cell))
        widths = [V / np.linalg.norm(np.cross(cell[j], cell[k]))
                  for i in range(3) for (j, k) in [[x for x in range(3) if x != i]]]
        return 0.5 * min(widths) - margin

    cmax = round(_max_safe_cutoff(supercell_atoms, C["cutoff_margin"]), 2)
    if cmax <= 0:
        raise ValueError("超胞太小, 安全截断<=0，请增大 min_sc_length。")
    DO_KAPPA = bool(C["kappa"])
    if DO_KAPPA:
        fc3_cut = cmax if C["fc3_cutoff"] is None else min(float(C["fc3_cutoff"]), cmax)
        CUTOFFS_ANG = [cmax, fc3_cut]
        logging.info("[拟合] 同时拟合 fc2+fc3, CUTOFFS_ANG = %s Å (fc2,fc3)", CUTOFFS_ANG)
    else:
        CUTOFFS_ANG = [cmax]
        logging.info("[拟合] 仅 fc2(只出声子谱), 二阶 cutoff = %.2f Å", cmax)
    if cmax < 3.5:
        logging.warning("  二阶 cutoff %.2f Å 偏小, 可能只含最近邻", cmax)

    disps_data = np.load(dft_dir / "dataset_disps.npy")
    forces_data = np.load(dft_dir / "dataset_forces.npy")
    assert disps_data.shape == forces_data.shape
    eq_frame, f_eq = disps_data[-1], forces_data[-1]

    # 自检：拟合前断言「dataset 末帧 = supercell_atoms 且同序」（容差 1e-3）。
    # 这是对「超胞原子序错位」bug（pymatgen 序≠phonopy 序）的最后一道防线：
    # 即便将来有人再把 supercell 换成其它构造器，错位也会在这里立即报错而不是
    # 静默产出离谱的 κ（详见 BUG_超胞原子序错位.md）。
    sc_spos = supercell_atoms.get_scaled_positions()
    is_frac_eq = (float(eq_frame.min()) >= -0.15 and float(eq_frame.max()) <= 1.15)
    if is_frac_eq:
        d_chk = eq_frame - sc_spos; d_chk -= np.round(d_chk)
        max_diff = float(np.abs(d_chk).max())
    else:
        # 末帧是 Cartesian：折算回分数坐标比对
        sc_cell_chk = np.asarray(supercell_atoms.cell.array if hasattr(supercell_atoms.cell, "array")
                                 else supercell_atoms.cell)
        eq_frac = np.linalg.solve(sc_cell_chk.T, eq_frame.T).T
        d_chk = eq_frac - sc_spos; d_chk -= np.round(d_chk)
        max_diff = float(np.abs(d_chk).max())
    if max_diff > 1e-3:
        raise RuntimeError(
            f"[超胞原子序自检] 数据集末帧与 supercell_atoms 不同序，max|Δfrac|={max_diff:.4f}。"
            "极可能是超胞构造器换回了 pymatgen / ASE.repeat() 等非 phonopy 序的实现，"
            "会让 hiphive 拟合的 fc2/fc3 贴到错误原子，导致 κ 系统性崩塌。"
            "请确认步骤 4 使用 make_supercell_phonopy（详见 BUG_超胞原子序错位.md）。")
    logging.info("[自检] 数据集末帧 ↔ supercell_atoms 同序 OK，max|Δfrac|=%.2e", max_diff)

    dmin, dmax, dmean = float(disps_data.min()), float(disps_data.max()), float(disps_data.mean())
    is_fractional = (dmin >= -0.15) and (dmax <= 1.15) and (0.2 < dmean < 0.8)
    sc_cell = np.asarray(supercell_atoms.cell.array if hasattr(supercell_atoms.cell, "array") else supercell_atoms.cell)
    if is_fractional:
        dfrac = disps_data - eq_frame[None]; dfrac -= np.round(dfrac)
        cart = np.einsum("nij,jk->nik", dfrac, sc_cell)
    else:
        cart = disps_data
    disps_use = (cart - cart[-1][None])[:-1]
    forces_use = (forces_data - f_eq[None])[:-1]
    rms = float(np.sqrt(np.mean(disps_use ** 2)))
    logging.info("已扣平衡力 (max|F_eq|=%.4f)，拟合 %d 帧，RMS位移=%.4f Å",
                 np.linalg.norm(f_eq, axis=1).max(), len(disps_use), rms)
    if rms > 0.5:
        logging.warning("  RMS位移 %.3f 异常偏大，核对坐标单位", rms)

    cs = ClusterSpace(prim, CUTOFFS_ANG)
    logging.info("ClusterSpace: %s", cs)
    scon = StructureContainer(cs)
    for d, f in zip(disps_use, forces_use):
        at = supercell_atoms.copy()
        at.new_array("displacements", d); at.new_array("forces", f)
        scon.add_structure(at)
    opt = Optimizer(scon.get_fit_data(), fit_method=C["fit_method"], train_size=C["fit_train_size"])
    opt.train()
    logging.info("Optimizer: %s", opt)

    params = enforce_rotational_sum_rules(cs, opt.parameters, C["rotational_rules"])
    fcp = ForceConstantPotential(cs, params)
    fcs = fcp.get_force_constants(supercell_atoms)

    # 6_fcs 留档 + 出谱用 fc2.hdf5；kappa 时一并写 fc3.hdf5
    fcs_dir = SUBDIRS["fcs"]
    fcp.write(str(fcs_dir / "model.fcp"))
    fc2_path = SUBDIRS["band-dft-cpu"] / "fc2.hdf5"
    fcs.write_to_phonopy(str(fc2_path), format="hdf5")
    fcs.write_to_phonopy(str(fcs_dir / "fc2.hdf5"), format="hdf5")
    if DO_KAPPA:
        fcs.write_to_phono3py(str(fcs_dir / "fc3.hdf5"))
        fcs.write_to_phono3py(str(SUBDIRS["band-dft-cpu"] / "fc3.hdf5"))   # 展示用，与 fc2 同目录
    (fcs_dir / "fit_summary.txt").write_text(
        "# 随机位移(hiPhive) 拟合摘要\n"
        f"method            = random\n"
        f"同时拟合 fc3      = {DO_KAPPA}\n"
        f"CUTOFFS_ANG       = {CUTOFFS_ANG}\n"
        f"拟合用帧数        = {len(disps_use)}\n"
        f"RMS 位移 (A)      = {rms:.4f}\n"
        f"fit_method        = {C['fit_method']}\n"
        f"\n[ClusterSpace]\n{cs}\n\n[Optimizer]\n{opt}\n", encoding="utf-8")
    logging.info("6_fcs 已留档: model.fcp + fc2.hdf5%s + fit_summary.txt",
                 " + fc3.hdf5" if DO_KAPPA else "")

    unitcell_path = SUBDIRS["band-dft-cpu"] / "POSCAR-unitcell"
    ase_write(str(unitcell_path), prim, format="vasp", sort=False)
    phonon = phonopy_load(unitcell_filename=str(unitcell_path),
                          supercell_matrix=np.diag(SUPERCELL),
                          primitive_matrix="auto",
                          force_constants_filename=str(fc2_path))
    result = compute_band_and_check(phonon, SUBDIRS["band-dft-cpu"], C, nac_vasprun)
    return result, fcs





# ============================================================================
# 步骤 9：findiff 有限位移法
# ============================================================================
def fit_fc2_findiff(SUPERCELL, dft_dir, SUBDIRS, used_indices, C, nac_vasprun):
    step = "步骤9 phonopy 有限位移求 fc2 + 声子谱"
    logging.info("=" * 60); logging.info("[%s]", step)
    from phonopy import load as phonopy_load
    disp_yaml = SUBDIRS["band-dft-cpu"] / "phonopy_disp.yaml"
    if not disp_yaml.is_file():
        raise RuntimeError(f"{disp_yaml} 不存在，无法 reload 有限位移 dataset")
    phonon = phonopy_load(str(disp_yaml), produce_fc=False, log_level=0)
    n_disp = len(phonon.supercells_with_displacements)

    forces_data = np.load(dft_dir / "dataset_forces.npy")
    f_eq = forces_data[-1]
    forces_disp = forces_data[:-1] - f_eq[None]
    used_indices = list(np.load(dft_dir / "used_indices.npy")) \
        if (dft_dir / "used_indices.npy").is_file() else list(range(1, n_disp + 1))

    if forces_disp.shape[0] != n_disp or used_indices != list(range(1, n_disp + 1)):
        missing = sorted(set(range(1, n_disp + 1)) - set(int(x) for x in used_indices))
        raise RuntimeError(
            f"有限位移法要求 {n_disp} 帧全齐:每个对称不等价位移都必需,缺任一帧都无法用有限差分"
            f"重建 fc2,不能像随机位移法那样跳过。实得 {forces_disp.shape[0]} 帧,缺号={missing[:20]}。"
            "请补算缺帧(增大 dft_retry 自动重投,或手动重跑对应 5_dft/POSCAR-* 目录)后重来;"
            "若要容忍失败帧,请改用 method='random'。")

    logging.info("灌入 %d 帧力（已扣残余力 max|F_eq|=%.4f），求 fc2",
                 n_disp, np.linalg.norm(f_eq, axis=1).max())
    phonon.forces = forces_disp
    phonon.produce_force_constants()

    try:
        from phonopy.file_IO import write_force_constants_to_hdf5
        fc2_path = SUBDIRS["band-dft-cpu"] / "fc2.hdf5"
        write_force_constants_to_hdf5(phonon.force_constants, filename=str(fc2_path))
        # 6_fcs：保存拟合的「输入摘要 + 输出 fc2」，使该目录自包含
        fcs_dir = SUBDIRS["fcs"]
        write_force_constants_to_hdf5(phonon.force_constants, filename=str(fcs_dir / "fc2.hdf5"))
        (fcs_dir / "fit_summary.txt").write_text(
            "# phonopy 有限位移 fc2 摘要（输入 + 输出）\n"
            "method            = findiff\n"
            f"输入数据集(5_dft) : {dft_dir / 'dataset_forces.npy'}\n"
            f"位移定义(7_band)  : {disp_yaml}\n"
            f"位移帧数          = {n_disp}\n"
            f"残余力 max|F_eq|  = {np.linalg.norm(f_eq, axis=1).max():.4f}\n"
            "输出              = fc2.hdf5\n",
            encoding="utf-8")
        logging.info("6_fcs 已留档: fc2.hdf5 + fit_summary.txt")
        logging.info("fc2.hdf5 已导出: %s", fc2_path)
    except Exception as e:
        logging.warning("fc2.hdf5 导出失败（不影响出谱）: %s", e)

    return compute_band_and_check(phonon, SUBDIRS["band-dft-cpu"], C, nac_vasprun)


# ============================================================================
# 步骤 9（findiff + kappa）：phono3py 0.03 单批 → 同时 produce fc2 + fc3
#   标准 phono3py 主流：一批 0.03 位移共用，先用该 fc2 出声子谱、判虚频，
#   再把同批的 fc2+fc3 交给步骤10 算 κ（与 random 的 rattle-0.03 单批拟合对称）。
#   reload 步骤5+6 存的 phono3py_disp.yaml，按完整 dataset 长度回填力（cutoff 砍掉
#   的 None 槽位填零，phono3py 据 dataset 自行忽略）。
# ============================================================================
def fit_fc2fc3_findiff(prim, SUPERCELL, dft_dir, SUBDIRS, C, nac_vasprun):
    step = "步骤9 phono3py 有限位移：一批 produce fc2 + fc3 → 声子谱"
    logging.info("=" * 60); logging.info("[%s]", step)
    from phono3py import load as phono3py_load
    from phono3py.file_IO import write_fc2_to_hdf5, write_fc3_to_hdf5
    from phonopy import load as phonopy_load

    disp_yaml = SUBDIRS["band-dft-cpu"] / "phono3py_disp.yaml"
    if not disp_yaml.is_file():
        raise RuntimeError(f"{disp_yaml} 不存在，无法 reload phono3py 三阶位移 dataset")
    ph3 = phono3py_load(str(disp_yaml), produce_fc=False, is_nac=False, log_level=0)
    all_scs = ph3.supercells_with_displacements
    n_total = len(all_scs)
    natom = len(ph3.supercell)
    todo_idx0 = [i for i, s in enumerate(all_scs) if s is not None]   # 0-based 应算槽位
    n_todo = len(todo_idx0)

    # 收集步骤8 的力数据集：末帧=平衡帧；前面各帧对应实际算出的（非连续）位移目录
    forces_data = np.load(dft_dir / "dataset_forces.npy")
    f_eq = forces_data[-1]
    forces_used = forces_data[:-1] - f_eq[None]                       # 扣平衡帧残余力
    used_indices = list(np.load(dft_dir / "used_indices.npy")) \
        if (dft_dir / "used_indices.npy").is_file() else list(range(1, n_total + 1))
    if len(used_indices) != forces_used.shape[0]:
        raise RuntimeError(f"used_indices({len(used_indices)}) 与力帧数({forces_used.shape[0]}) 不一致")

    # 强校验：实际算出的目录号(1-based) 必须恰好等于应算槽位(1-based)，不允许缺帧
    expect_1based = sorted(i + 1 for i in todo_idx0)
    got_1based = sorted(int(x) for x in used_indices)
    if got_1based != expect_1based:
        missing = sorted(set(expect_1based) - set(got_1based))
        extra = sorted(set(got_1based) - set(expect_1based))
        raise RuntimeError(
            f"三阶位移帧不齐：应算 {n_todo} 帧，实得 {len(got_1based)} 帧；"
            f"缺号={missing[:10]} 多号={extra[:10]}。有限位移法每帧都必需、不能跳过;"
            "请补算缺帧(增大 dft_retry 自动重投,或手动重跑对应目录)后重跑,"
            "或改用 method='random' 以容忍失败帧。")

    # 按完整 dataset 长度回填：None 槽位保持 0；phono3py 据 dataset 自动忽略
    forces_full = np.zeros((n_total, natom, 3))
    for k, idx1 in enumerate(used_indices):
        forces_full[int(idx1) - 1] = forces_used[k]
    logging.info("灌入 %d/%d 帧力（已扣残余力 max|F_eq|=%.4f），produce fc2 + fc3",
                 n_todo, n_total, np.linalg.norm(f_eq, axis=1).max())

    ph3.forces = forces_full
    ph3.produce_fc3(symmetrize_fc3r=True)
    ph3.produce_fc2(symmetrize_fc2=True, is_compact_fc=False)   # full fc2，可直接喂 phonopy
    fc2_full = ph3.fc2

    # 落盘 fc2/fc3（7_band 主存 + 6_fcs 留档）；fc2 用 full 格式，_solve_phono3py 兼容
    fc2_path, fc3_path = SUBDIRS["band-dft-cpu"] / "fc2.hdf5", SUBDIRS["band-dft-cpu"] / "fc3.hdf5"
    write_fc2_to_hdf5(fc2_full, filename=str(fc2_path))
    write_fc3_to_hdf5(ph3.fc3, filename=str(fc3_path))
    try:
        fcs_dir = SUBDIRS["fcs"]
        write_fc2_to_hdf5(fc2_full, filename=str(fcs_dir / "fc2.hdf5"))
        write_fc3_to_hdf5(ph3.fc3, filename=str(fcs_dir / "fc3.hdf5"))
        (fcs_dir / "fit_summary.txt").write_text(
            "# phono3py 有限位移 fc2+fc3 摘要（一批 0.03 共用）\n"
            "method            = findiff (kappa)\n"
            f"输入数据集(5_dft) : {dft_dir / 'dataset_forces.npy'}\n"
            f"位移定义(7_band)  : {disp_yaml}\n"
            f"位移幅度          = {float(C['findiff_fc3_disp'])} Å\n"
            f"cutoff_pair       = {C['findiff_fc3_cutoff_pair']} Å\n"
            f"dataset 总槽位    = {n_total}\n"
            f"实算位移帧        = {n_todo}\n"
            f"残余力 max|F_eq|  = {np.linalg.norm(f_eq, axis=1).max():.4f}\n"
            "输出              = fc2.hdf5 + fc3.hdf5\n",
            encoding="utf-8")
        logging.info("6_fcs 已留档: fc2.hdf5 + fc3.hdf5 + fit_summary.txt")
    except Exception as e:
        logging.warning("6_fcs 留档失败（不影响出谱/κ）: %s", e)
    logging.info("fc2.hdf5 + fc3.hdf5 已导出: %s", SUBDIRS["band-dft-cpu"])

    # 声子谱：full fc2 直接灌入 phonopy（与判稳、与 κ 用的 fc2 完全同一个）
    unitcell_path = SUBDIRS["band-dft-cpu"] / "POSCAR-unitcell"
    phonon = phonopy_load(unitcell_filename=str(unitcell_path),
                          supercell_matrix=np.diag(SUPERCELL),
                          primitive_matrix="auto", produce_fc=False, log_level=0)
    phonon.force_constants = fc2_full
    result = compute_band_and_check(phonon, SUBDIRS["band-dft-cpu"], C, nac_vasprun)
    return result, (fc2_path, fc3_path)


# ████████████████████████████████████████████████████████████████████████████
# ██  热导率（kappa）模块                                                     ██
# ██  fc3 来源随 method：                                                      ██
# ██    random →rattle 0.03 单批拟合 fc2+fc3（零额外 DFT）；                    ██
# ██    findiff→phono3py 0.03 单批 produce fc2+fc3（与声子谱共用同一批，         ██
# ██            步骤9 已产出，本模块直接复用，无第二批 DFT）。                   ██
# ██  求解器由 kappa_solver 选：phono3py（BTE-RTA+Wigner）/ shengbte（迭代BTE）。██
# ████████████████████████████████████████████████████████████████████████████

def _kappa_temps(C):
    t0, t1, dt = float(C["kappa_t_min"]), float(C["kappa_t_max"]), float(C["kappa_t_step"])
    n = int(round((t1 - t0) / dt)) + 1
    return [round(t0 + i * dt, 3) for i in range(n)]


# ────────────────────────────────────────────────────────────────────────────
# fc2+fc3 来源（两条路径都在步骤9 已产出，本模块零额外 DFT、直接复用）：
#   random →hiphive rattle 0.03 单批拟合 fc2+fc3（fcs 对象）
#   findiff→phono3py 0.03 单批 produce fc2+fc3（fit_fc2fc3_findiff 写出 fc2/fc3.hdf5）
# ────────────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────────────
# 求解器 A：phono3py BTE-RTA（+Wigner，四面体法，同位素，可选 NAC）
# ────────────────────────────────────────────────────────────────────────────
def _solve_phono3py(C, prim, SUPERCELL, kappa_dir, fc2_path, fc3_path, use_nac):
    import os, h5py
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from phono3py import Phono3py
    from phonopy.structure.atoms import PhonopyAtoms

    unit = PhonopyAtoms(symbols=prim.get_chemical_symbols(),
                        scaled_positions=prim.get_scaled_positions(),
                        cell=prim.cell[:])
    ph3 = Phono3py(unit, supercell_matrix=np.diag(SUPERCELL),
                   primitive_matrix="auto", cutoff_frequency=C["kappa_cutoff_freq"])
    with h5py.File(fc2_path, "r") as fh:
        fc2_data = fh["fc2"][:] if "fc2" in fh else fh["force_constants"][:]
    with h5py.File(fc3_path, "r") as fh:
        fc3_data = fh["fc3"][:]
    try:
        from phono3py.phonon3.fc3 import (set_permutation_symmetry_fc3,
                                          set_translational_invariance_fc3)
        set_permutation_symmetry_fc3(fc3_data); set_translational_invariance_fc3(fc3_data)
    except Exception as e:
        logging.warning("fc3 对称化跳过: %s", e)
    ph3.fc2, ph3.fc3 = fc2_data, fc3_data

    if use_nac:
        from phonopy.file_IO import parse_BORN
        born = kappa_dir / "BORN"
        if not born.is_file():
            raise FileNotFoundError(f"phono3py NAC 需 {born}（请放置 BORN 文件）")
        ph3.nac_params = parse_BORN(ph3.phonon_primitive, filename=str(born))

    temps = _kappa_temps(C)
    ph3.mesh_numbers = C["kappa_mesh"]
    ph3.sigmas = [None]   # 四面体法
    ph3.init_phph_interaction()
    mesh_str = "".join(map(str, C["kappa_mesh"]))
    cwd = os.getcwd(); os.chdir(str(kappa_dir))
    try:
        kw = dict(temperatures=np.array(temps, dtype="double"),
                  is_isotope=C["kappa_isotope"], is_LBTE=False, write_kappa=True)
        if C["kappa_wigner"]:
            try:
                ph3.run_thermal_conductivity(**kw, is_wigner=True)
            except TypeError:
                logging.warning("phono3py 不支持 is_wigner，回退标准 RTA")
                ph3.run_thermal_conductivity(**kw)
        else:
            ph3.run_thermal_conductivity(**kw)
    finally:
        os.chdir(cwd)

    cand = sorted(kappa_dir.glob(f"kappa-m{mesh_str}*.hdf5"))
    if not cand:
        raise FileNotFoundError("未找到 kappa-m*.hdf5")
    kpath = cand[-1]

    def _T6(arr):
        if arr is None: return None
        arr = np.asarray(arr)
        if arr.ndim == 2 and arr.shape[1] >= 6: return arr[:, :6]
        return arr.reshape(arr.shape[0], -1, arr.shape[-1]).sum(axis=1)[:, :6]

    with h5py.File(kpath, "r") as f:
        temps_out = f["temperature"][:]; keys = set(f.keys())
        if "kappa_TOT_RTA" in keys:
            k_tot = _T6(f["kappa_TOT_RTA"][:])
            k_p = _T6(f["kappa_P_RTA"][:]) if "kappa_P_RTA" in keys else None
            k_c = _T6(f["kappa_C"][:]) if "kappa_C" in keys else None
        else:
            k_tot, k_p, k_c = _T6(f["kappa"][:]), None, None

    lines = [f"求解器: phono3py | Mesh={C['kappa_mesh']} | 四面体法 | "
             f"isotope={C['kappa_isotope']} | wigner={'on' if k_c is not None else 'off'}",
             "温度(K)    κ_xx        κ_yy        κ_zz        平均κ (W/m·K)", "-" * 60]
    for i, T in enumerate(temps_out):
        kx, ky, kz = k_tot[i, 0], k_tot[i, 1], k_tot[i, 2]
        lines.append(f"{T:6.0f}    {kx:8.3f}    {ky:8.3f}    {kz:8.3f}    {(kx+ky+kz)/3:8.3f}")
    txt = kappa_dir / "kappa_result.txt"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logging.info("热导率结果:\n%s", "\n".join(lines))

    plt.figure(figsize=(6, 4.2))
    plt.plot(temps_out, np.mean(k_tot[:, :3], axis=1), "o-", label="κ_TOT (avg)")
    if k_c is not None:
        plt.plot(temps_out, np.mean(k_p[:, :3], axis=1), "s--", lw=1, label="κ_P")
        plt.plot(temps_out, np.mean(k_c[:, :3], axis=1), "^--", lw=1, label="κ_C")
    plt.xlabel("Temperature (K)"); plt.ylabel(r"$\kappa_{lat}$ (W/m·K)")
    plt.legend(); plt.grid(alpha=0.3); plt.tight_layout()
    fig = kappa_dir / "kappa_fig.png"; plt.savefig(str(fig), dpi=150)
    return {"kappa_result": str(txt), "kappa_fig": str(fig), "kappa_solver": "phono3py"}


# ────────────────────────────────────────────────────────────────────────────
# 求解器 B：ShengBTE（迭代 BTE/CONV，同位素，可选 NAC）
# ────────────────────────────────────────────────────────────────────────────
def _write_shengbte_control(C, atoms, SUPERCELL, out_path, use_nac):
    from ase.data import chemical_symbols as _cs
    cell = np.array(atoms.cell)
    numbers = atoms.numbers; unique = sorted(set(numbers))
    syms = [_cs[z] for z in unique]; kd = {z: i + 1 for i, z in enumerate(unique)}
    types = [kd[z] for z in numbers]; spos = atoms.get_scaled_positions(wrap=True)
    scell = [int(x) for x in SUPERCELL]; ng = C["kappa_mesh"]
    L = ["&allocations", f"  nelements={len(unique)},", f"  natoms={len(atoms)},",
         f"  ngrid(:)={ng[0]} {ng[1]} {ng[2]}", "&end", "&crystal", "  lfactor=0.1,"]
    for r in range(3):
        L.append(f"  lattvec(:,{r+1})=" + " ".join(f"{cell[r,i]:.10f}" for i in range(3)) + ",")
    L.append("  elements=" + " ".join(f'"{s}"' for s in syms))
    L.append("  types=" + " ".join(str(t) for t in types) + ",")
    for idx, p in enumerate(spos):
        L.append(f"  positions(:,{idx+1})=" + " ".join(f"{x:.10f}" for x in p) + ",")
    L.append(f"  scell(:)={scell[0]} {scell[1]} {scell[2]}")
    L += ["&end", "&parameters", f"  T_min={float(C['kappa_t_min']):.1f}",
          f"  T_max={float(C['kappa_t_max']):.1f}", f"  T_step={float(C['kappa_t_step']):.1f}",
          f"  scalebroad={C['kappa_scalebroad']}", "&end", "&flags",
          f"  autoisotopes={'T' if C['kappa_isotope'] else 'F'},",
          f"  convergence={'T' if C['kappa_convergence'] else 'F'},",
          f"  nonanalytic={'T' if use_nac else 'F'},", "  nanowires=F,", "&end"]
    out_path.write_text("\n".join(L) + "\n", encoding="utf-8")
    logging.info("CONTROL 已写出: %s", out_path)


def _parse_shengbte_kappa(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 10:
            rows.append([float(x) for x in parts[:10]])
    if not rows:
        raise ValueError(f"无法解析 {path}")
    data = np.array(rows)
    return data[:, 0], data[:, 1:]


def _solve_shengbte(C, prim, SUPERCELL, kappa_dir, fcs, use_nac, config):
    """ShengBTE 求解。FORCE_CONSTANTS_2ND/3RD 用 hiphive 原生导出（格式即 ShengBTE 规范）：
       2ND = fcs.write_to_phonopy(text)（full phonopy 格式）
       3RD = fcs.write_to_shengBTE(prim)（块格式：块数 + 每块晶格矢量+原子三元组+27 分量）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ase_write(str(kappa_dir / "POSCAR"), prim, format="vasp", direct=True, sort=False)
    # hiphive write_to_shengBTE 内部 Structure 要求分数坐标严格 < 1，
    # 但 fcs.supercell 偶尔会因数值噪声出现 0.9999999... 触发 ValueError('bad spos ...')。
    # 注意 fcs.supercell 是 .copy() 返回，要写到底层 _supercell；prim 直接 wrap 即可。
    def _safe_wrap_spos(atoms, eps=1e-9):
        frac = atoms.get_scaled_positions(wrap=True)
        frac = np.where(frac >= 1.0 - eps, 0.0, frac)
        frac = np.where(frac < eps, 0.0, frac)
        atoms.set_scaled_positions(frac)
    _safe_wrap_spos(fcs._supercell)
    _safe_wrap_spos(prim)
    fcs.write_to_phonopy(str(kappa_dir / "FORCE_CONSTANTS_2ND"), format="text")
    fcs.write_to_shengBTE(str(kappa_dir / "FORCE_CONSTANTS_3RD"), prim)
    logging.info("[kappa/shengbte] FORCE_CONSTANTS_2ND/3RD 已由 hiphive 原生导出")
    _write_shengbte_control(C, prim, SUPERCELL, kappa_dir / "CONTROL", use_nac)
    if use_nac:
        logging.warning("ShengBTE NAC：CONTROL 已置 nonanalytic=T，但未自动写入 born/epsilon；"
                        "极性材料请手动在 CONTROL 补 Born 有效电荷与介电张量，否则 ShengBTE 报错")

    for f in ("CONTROL", "FORCE_CONSTANTS_2ND", "FORCE_CONSTANTS_3RD", "POSCAR"):
        if not (kappa_dir / f).is_file():
            raise FileNotFoundError(f"ShengBTE 缺输入: {f}")
    hpc.submit_shengbte(str(kappa_dir), C["shengbte_exe"], config)

    conv = kappa_dir / "BTE.KappaTensorVsT_CONV"
    rta = kappa_dir / "BTE.KappaTensorVsT_RTA"
    if not conv.exists() and not rta.exists():
        raise FileNotFoundError("未找到 BTE.KappaTensorVsT_CONV/_RTA，ShengBTE 可能失败")
    rf = conv if conv.exists() else rta
    tag = "iterative BTE (CONV)" if conv.exists() else "RTA"
    temps_out, kt = _parse_shengbte_kappa(rf)
    kxx, kyy, kzz = kt[:, 0], kt[:, 4], kt[:, 8]
    lines = [f"求解器: ShengBTE ({tag}) | ngrid={C['kappa_mesh']} | "
             f"isotope={C['kappa_isotope']} | scalebroad={C['kappa_scalebroad']}",
             "温度(K)    κ_xx        κ_yy        κ_zz        平均κ (W/m·K)", "-" * 60]
    for i, T in enumerate(temps_out):
        lines.append(f"{T:6.0f}    {kxx[i]:8.3f}    {kyy[i]:8.3f}    {kzz[i]:8.3f}    "
                     f"{(kxx[i]+kyy[i]+kzz[i])/3:8.3f}")
    txt = kappa_dir / "kappa_result.txt"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logging.info("热导率结果:\n%s", "\n".join(lines))

    plt.figure(figsize=(6, 4.2))
    plt.plot(temps_out, (kxx + kyy + kzz) / 3, "o-", label=f"κ ({tag})")
    plt.xlabel("Temperature (K)"); plt.ylabel(r"$\kappa_{lat}$ (W/m·K)")
    plt.legend(); plt.grid(alpha=0.3); plt.tight_layout()
    fig = kappa_dir / "kappa_fig.png"; plt.savefig(str(fig), dpi=150)
    return {"kappa_result": str(txt), "kappa_fig": str(fig), "kappa_solver": "shengbte"}


# ────────────────────────────────────────────────────────────────────────────
# 调度入口（在 run() 步骤9 之后、结构稳定时调用）
# ────────────────────────────────────────────────────────────────────────────
def compute_kappa(C, prim, supercell_atoms, SUPERCELL, SYSTEM, dft_dir, SUBDIRS,
                  method, config, fcs_random=None, fc_paths_findiff=None):
    step = "步骤10 晶格热导率"
    if C["kappa_solver"] not in ("shengbte", "phono3py"):
        raise ValueError(f"kappa_solver 仅支持 'shengbte'/'phono3py'，收到 {C['kappa_solver']!r}")
    # findiff(phono3py compact fc3) 无可靠的 ShengBTE 转换器：先挡住，避免建空目录/产出错格式
    if method == "findiff" and C["kappa_solver"] == "shengbte":
        raise ValueError(
            "findiff(phono3py 三阶有限位移)的 compact fc3 没有可靠的 ShengBTE 导出口；"
            "请对 findiff 用 kappa_solver='phono3py'，或对 ShengBTE 用 method='random'。")
    logging.info("=" * 60); logging.info("[%s] solver=%s, fc3 来源=%s",
                 step, C["kappa_solver"], method)
    kappa_dir = SUBDIRS["kappa"]; kappa_dir.mkdir(parents=True, exist_ok=True)
    use_nac = C["nac"] if C["kappa_nac"] is None else bool(C["kappa_nac"])
    solver = C["kappa_solver"]

    # NAC：声子谱阶段已从 DFPT 静态生成 BORN（含 ε∞+Born，见 compute_band_and_check）。
    #   phono3py 求解器需 kappa_dir/BORN；此处把已生成的 BORN 自动带过来，避免要求用户手放。
    #   若上游 NAC 读取失败没生成 BORN，则保持原有“缺 BORN 报错”行为。
    if use_nac:
        src_born = SUBDIRS["band-dft-cpu"] / "BORN"
        dst_born = kappa_dir / "BORN"
        if src_born.is_file() and not dst_born.is_file():
            shutil.copy2(str(src_born), str(dst_born))
            logging.info("[kappa] 已复用声子谱阶段生成的 BORN（介电/Born）: %s", dst_born)

    if method == "random":
        # 直接复用步骤9 已拟合的 fcs(含 fc2+fc3)，不再二次拟合
        if fcs_random is None:
            raise RuntimeError("random 路径缺 fcs（步骤9 应已拟合 fc2+fc3 并传入）")
        fcs = fcs_random
        fc2_path, fc3_path = kappa_dir / "fc2.hdf5", kappa_dir / "fc3.hdf5"
        fcs.write_to_phonopy(str(fc2_path), format="hdf5")
        fcs.write_to_phono3py(str(fc3_path))
        logging.info("[kappa/random] 复用步骤9 拟合的 fc2+fc3（零二次拟合）")
    else:  # findiff -> 仅 phono3py（shengbte 已被上面挡住）
        # fc2+fc3 已在步骤9 由「同一批 0.03」produce 出（与声子谱共用同一 fc2），直接复用
        if fc_paths_findiff is None:
            raise RuntimeError("findiff 路径缺 fc2/fc3（步骤9 fit_fc2fc3_findiff 应已产出并传入）")
        src_fc2, src_fc3 = fc_paths_findiff
        fc2_path, fc3_path = kappa_dir / "fc2.hdf5", kappa_dir / "fc3.hdf5"
        shutil.copy2(str(src_fc2), str(fc2_path))
        shutil.copy2(str(src_fc3), str(fc3_path))
        fcs = None
        logging.info("[kappa/findiff] 复用步骤9「同批 0.03」produce 的 fc2+fc3（零额外 DFT，谱/κ 同一 fc2）")

    if solver == "phono3py":
        return _solve_phono3py(C, prim, SUPERCELL, kappa_dir, fc2_path, fc3_path, use_nac)
    else:  # shengbte（此处必为 random，fcs 可用，用 hiphive 原生导出 ShengBTE 文件）
        return _solve_shengbte(C, prim, SUPERCELL, kappa_dir, fcs, use_nac, config)


if __name__ == "__main__":
    if ROOT.exists():
        shutil.rmtree(ROOT)
        logging.info("已清理旧目录: %s", ROOT)
    result = run({})
    logging.info("运行结果: %s", result)