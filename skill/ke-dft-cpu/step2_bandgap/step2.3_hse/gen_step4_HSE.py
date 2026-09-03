#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step4_HSE.py — standard HSE06（KPOINTS_OPT 方案，SOC 自动检测）
====================================================================
在父目录（含 step3_PBE_WAVECAR/）下运行，从 step3 一步搭建 step4_HSE_band 的输入。

SOC 自动检测（无手动开关，防止旋量/标量 WAVECAR 不匹配）:
    读 step3 INCAR 的 LSORBIT：
      .TRUE.  -> SOC 模式：submit_ncl.tpl / vasp_ncl，注入 LSORBIT/GGA_COMPAT/LMAXMIX，
                 NBANDS 与 MAGMOM 从 step3 继承
      其它/缺 -> 无 SOC：submit_std.tpl / vasp_std，SOC 相关标签
                 （LSORBIT/LNONCOLLINEAR/SAXIS/GGA_COMPAT）一律剔除
    ISPIN=2 一律拒绝（与 gen_step4_Band_structure.py 一致）。
    父目录需备好对应模板（只含 {{JOBNAME}} 占位符）。

做的事:
    1. 把 step3 的 KPOINTS（自洽均匀网格）原样拷到 step4。
    2. 把 step3 的 KPOINTS_OPT + kpath.json 处理后写入 step4：
       可选降采样（--line-density）与切片（--kpath-slice）。
    3. 把 step3 的 WAVECAR 拷到 step4（HSE 从预收敛波函数重启）。
    4. 把 POSCAR / POTCAR 原样拷到 step4。
    5. 从父目录模板渲染 submit.sh（SOC -> ncl，无 SOC -> std）。
    6. 读 step3 的 INCAR，继承全部键，按规则改写成 HSE 的 INCAR 写入 step4。

新增（针对 KPOINTS_OPT one-shot 太慢 / 无中途 checkpoint）:
    --line-density N     每条路径腿保留 N 个点（含两端高对称点），从 step3 的
                         kpath.json + KPOINTS_OPT 降采样。【默认开启，每腿 10 点】。
                         --line-density 0 关闭，用 step3 的完整路径。
                         HSE 成本对路径点数严格线性；出版级能带每腿 10~15 点足够。
    --kpath-slice i/n    只生成第 i 段（共 n 段），目录名 step4_HSE_band_p{i}of{n}，
                         各段共享同一 step3 WAVECAR、各自重跑一次便宜的 SCF，
                         KPOINTS_OPT 只含本段路径点 -> n 个作业并行。
    --kpath-slice all/n  一次生成全部 n 段目录。
    --link-wavecar       各目录用符号链接共享 step3 WAVECAR（省磁盘）。★ 注意：仅当
                         LWAVE=.FALSE. 时安全；本版 LWAVE=.TRUE.，VASP 会写自己的
                         WAVECAR，符号链接会透过链接覆盖 step3 源，故此选项会被
                         【自动忽略】、强制改为拷贝以保护 step3 源 WAVECAR。
    --kpar K             手动指定 KPAR（默认自动，上限 KPAR_MAX）。
    --jobname NAME       手动作业名（切片时自动加 _p{i}of{n} 后缀）。

    切片元数据写在各段 kpath.json 的 "slice" 字段（完整路径 + offset/count），
    gen_step4_Band_structure.py 用它把各段 vasprun.xml 拼回整条能带。

INCAR 改写（step3 PBE 预收敛 -> step4 HSE 能带）:
    删: ICHARG, IVDW 系列; 用 KPOINTS_OPT 时另删 NCORE/NPAR（其驱动要求 NCORE=1，
        写了 NPAR/NCORE>1 会 "I REFUSE TO CONTINUE WITH THIS SICK JOB"）。
    改/增: SYSTEM, ISTART=1, ISYM=3(HSE 默认，必须显式写，否则会继承 step3 的值),
           ALGO=Damped, TIME=0.4,
           LHFCALC=.TRUE., HFSCREEN=0.2, AEXX=0.25,
           HFRCUT=-1（仅当最终泛函是非屏蔽杂化时自动加，见 HFRCUT_MODE）,
           PRECFOCK=Fast, LORBIT=11（轨道投影，fat band-dft-cpu 必需）,
           LWAVE=.TRUE.（存波函数）, LCHARG=.TRUE.（存 CHGCAR）
    并行: 只注入 KPAR（KPOINTS_OPT 下 NCORE 固定 1，不写）。

用法:
    cd <父目录>      # 里面有 step3_PBE_WAVECAR/（已跑完，有 WAVECAR）
    python gen_step4_HSE.py                                  # 默认每腿 10 点
    python gen_step4_HSE.py --line-density 0                 # step3 完整路径
    python gen_step4_HSE.py --kpath-slice all/2             # 切两段（LWAVE=.TRUE. 下各段拷贝 WAVECAR）

依赖: 无（纯标准库）
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import require_dim, resolve_dim, resolve_tpl  # noqa: E402
import stepconf  # noqa: E402

# ============================== 配置 ==============================
STEP3_DIR  = "step2_bandgap/step2.2_pbe"   # 源目录
STEP4_DIR  = "step2_bandgap/step2.3_hse"      # 目标目录（切片时自动加 _p{i}of{n}）

INCAR_FILE = "INCAR"
OUTCAR_FILE = "OUTCAR"
COPY_FILES = ["KPOINTS", "POSCAR", "POTCAR"]   # WAVECAR / KPOINTS_OPT / kpath.json 单独处理
METHOD_FILE = "workflow_method.txt"
KPOPT_FILE  = "KPOINTS_OPT"
IBZKPT_FILE = "IBZKPT"
KPATH_FILE  = "kpath.json"

SUBMIT_TPL_NCL = "submit_ncl"   # SOC   -> vasp_ncl（按维度取 submit_ncl_2d/3d.tpl，回退 submit_ncl.tpl）
SUBMIT_TPL_STD = "submit_std"   # 无SOC -> vasp_std（同上）
SUBMIT_DEFAULTS = {
    "JOBNAME": None,      # None = 自动生成 <label>_s4HSE
}

# ---- submit.sh Slurm 参数覆盖（渲染模板后再补丁；None=不改，保持模板原值）----
# submit.sh 来源不变（仍从 submit_ncl/std 模板渲染）；只在渲染后覆盖三行：
#   #SBATCH --nodes= / --ntasks-per-node= / --qos=
SUBMIT_OVERRIDE = {
    "nodes":           None,
    "ntasks_per_node": None,
    "qos":             None,
}

# ---- 依据总 k 点数自动放大 ntasks-per-node ----
# 总 k 点数 = 自洽不可约点数（KPOINTS 自动网格时回读 step3 IBZKPT）
#          + 本目录实际写入的能带路径点数（已计入 --line-density 降采样 / --kpath-slice 切片）。
# 当该总数 > KPTS_TOTAL_THRESHOLD 时，认为作业较重，把 submit.sh 的
# --ntasks-per-node 覆盖成 NTASKS_PER_NODE_LARGE；未超过阈值则保持模板
# （及上面 SUBMIT_OVERRIDE）的原值不动。
# 若想按“step3 完整路径点数”而非本目录实际点数来判定，把 --line-density 0
# 关闭降采样、且不切片即可（此时本目录点数 = 完整路径点数）。
KPTS_TOTAL_THRESHOLD  = 100     # 阈值：总 k 点数【严格大于】此值才放大
NTASKS_PER_NODE_LARGE = 48     # 超阈值时写入 submit.sh 的 --ntasks-per-node

# ---- HSE 阶段的 DFT+U 处理（三态开关）----
# step1/2/3 若加了 U（LDAU*），会一路继承到 step3 INCAR。到 HSE 这一步怎么办，
# 文献上有分歧，故给三态开关，由你按体系与文献决定：
#   "remove"（默认，最主流、最安全）:
#       删掉所有 LDAU* 标签，做纯 HSE06。
#       依据：HSE 已通过精确交换修正 d/f 电子自作用误差，与 +U 功能重叠；
#             多数工作把 HSE 与 DFT+U 视为【互替】路线，不叠加。
#   "keep":
#       原样保留 step3 继承来的 U（即 HSE+U）。
#       依据：有文献（如 Aras & Kılıç, JCP 141, 044106 (2014)）主张二者【互补】，
#             对强局域态体系 HSE+U 可更好重现实验带隙——但 U 只应作用于局域态，
#             且 HSE+U 的最优 U* 通常需【重新拟合】，不等于 PBE+U 的 U。
#   "custom":
#       用下方 HSE_U_VALUES 为 HSE 单独指定 U（覆盖继承值）。
#       适用：你查到/拟合了专门用于 HSE 的 U*。表为空则等同 remove 并告警。
# 参考：https://doi.org/10.1063/1.4890458 （HSE+U for metal chalcogenides）
HSE_U_MODE = "remove"        # "remove" | "keep" | "custom"
# custom 模式用：每元素有效 U（eV）。d 元素 LDAUL=2、f 元素 LDAUL=3 自动判定。
HSE_U_VALUES = {}            # 例: {"Fe": 4.0}
# custom 模式判定 d/f 用（与 step1 同款；只用于决定 LDAUL）
HSE_D_ELEMS = {
    "Sc","Ti","V","Cr","Mn","Fe","Co","Ni","Cu","Zn",
    "Y","Zr","Nb","Mo","Tc","Ru","Rh","Pd","Ag","Cd",
    "La","Lu","Hf","Ta","W","Re","Os","Ir","Pt","Au","Hg",
    "Ga","Ge","In","Sn","Tl","Pb","Bi",
}
HSE_F_ELEMS = {
    "Ce","Pr","Nd","Pm","Sm","Eu","Gd","Tb","Dy","Ho","Er","Tm","Yb",
    "Ac","Th","Pa","U","Np","Pu","Am","Cm",
}

# INCAR 改写规则
# ---- 库仑奇点处理 (HFRCUT)：非屏蔽杂化 + 路径点 必须设 -1 ----
# VASP Wiki (KPOINTS_OPT) 明确警告：使用【长程】Hartree-Fock 交换的泛函
# （例如未屏蔽的杂化泛函），默认的库仑奇点处理方式 HFRCUT=0 并不适用于
# "没有参与构造 Fock 势的那些 k 点"，此时应改用 HFRCUT=-1。
# 而 KPOINTS_OPT 上的路径点恰好【全部】属于这一类：自洽只在 KPOINTS 的
# 均匀网格上做，Fock 势也只由那些点构造，路径点是事后 one-shot 上去的。
# 后果极其隐蔽 —— 不报错、不警告，路径上的本征值系统性偏移，能带图照样好看。
#
# ★ 判据是【有没有屏蔽】，不是 AEXX 的数值：
#     HFSCREEN > 0      -> 屏蔽型 (HSE06/HSE03…)，长程部分已被截掉，无需 HFRCUT
#     LTHOMAS = .TRUE.  -> Thomas-Fermi 屏蔽，同上
#     两者都没有         -> 非屏蔽 (PBE0 / 纯 HF / 自定义非屏蔽杂化) -> 必须 HFRCUT=-1
#   只调 AEXX 而保留 HFSCREEN=0.2 仍然是屏蔽的，不需要 HFRCUT。
#   把 HFSCREEN 从 INCAR_SET 里删掉（或设成 0）才会触发。
#
# "auto" = 按上面判据自动决定（推荐）
# 整数    = 强制写该值
# None    = 永不写 HFRCUT（自担风险）
HFRCUT_MODE = "auto"

# ###################################################################
# ★ 杂化泛函族 / 色散 / 对称性 ★
# ###################################################################

# ================= 用哪个杂化泛函 =================
# VASP 里杂化泛函 = 半局域部分(GGA) + 精确交换(LHFCALC/AEXX/HFSCREEN)。
# 半局域部分继承上游还是钉死成 PBE，决定了你算的到底是哪个泛函：
#
#   上游 GGA=PE (pbe / pbe-d3)  + AEXX=0.25 + HFSCREEN=0.2  ->  HSE06
#   上游 GGA=PS (pbesol)        + AEXX=0.25 + HFSCREEN=0.2  ->  HSEsol
#       （Schimka, Harl & Kresse, J. Chem. Phys. 134, 024116 (2011)）
#
# "auto"   跟随上游：PE->HSE06，PS->HSEsol。几何与能带用同一族泛函，自洽。
#          ★ 推荐，也是本次修正的默认值
# "hse06"  无论上游是什么，一律 GGA=PE（旧行为）。好处是与绝大多数文献可比；
#          代价是几何来自 PBEsol 而能带来自 PBE 基，两者不是同一泛函。
# "hsesol" 一律 GGA=PS。
HYBRID_FLAVOR = "auto"     # "auto" | "hse06" | "hsesol"

# ================= 色散修正 IVDW =================
# ★ 先明确一点：D2/D3 是加在【总能与力】上的原子对势，不进哈密顿量，
#   所以它对本征值、带隙、能带形状【没有任何影响】。本步只出能带，
#   删掉它得到的能带和保留它是逐位相同的。
#
# "remove"  删除 IVDW/VDW_* （默认，最稳）。避免某些 VASP 版本对
#           "HSE06 + D3" 找不到内置 D3 参数时报警或中止。
# "inherit" 原样保留。只有当你还想让 step4 的总能与色散一致时才需要 —— 但
#           注意 D3 参数是按泛函标定的（PBE-D3 与 HSE06-D3 的 s8/a1/a2 不同），
#           VASP 会自行按 LHFCALC/GGA 重选参数，跑完请 grep OUTCAR 的
#           VDW_S8/VDW_A1/VDW_A2 确认它选对了。
STEP4_VDW = "remove"       # "remove" | "inherit"

# ================= 对称性 ISYM =================
# "auto" -> 3，这是 LHFCALC=.TRUE. 时 VASP 自己的默认值。
#   ISYM=3 的含义：不直接对称化电荷密度，而是把不可约区 k 点上的轨道用对称操作
#   旋转出来构造密度 —— Fock 交换要在整个 BZ 上对 q 求和，这是唯一正确的做法。
#   ISYM=2 与 ISYM=3 给出的不可约 k 点集相同，所以 step3(ISYM=2) 的 WAVECAR
#   与本步(ISYM=3) 完全兼容，NKPTS 不变。
#
# 什么时候不该用 3：加外电场 / 偶极修正 / Berry 相极化时要用 0。此时 step3 也
#   必须同为 0（STEP3_ISYM="0"），且两步的 KPOINTS 都得是完整网格，否则
#   WAVECAR 的 k 点数对不上，ISTART=1 会读失败。
#   非共线 SOC：VASP >= 5.4.1 支持非共线对称性，ISYM=3 通常可用；求稳可两步同设 0。
STEP4_ISYM = "auto"        # "auto" | "0" | "1" | "2" | "3"
# ###################################################################

INCAR_REMOVE = ["ICHARG"]
INCAR_REMOVE_VDW = ["IVDW", "VDW_S6", "VDW_S8", "VDW_SR", "VDW_A1", "VDW_A2"]
INCAR_REMOVE_NOSOC = ["LSORBIT", "LNONCOLLINEAR", "SAXIS", "GGA_COMPAT"]  # 无 SOC 时剔除
INCAR_SET = {
    "SYSTEM":   "HSE06 band-dft-cpu (step4): read step3 WAVECAR, KPOINTS_OPT path",
    "ISTART":   "1",          # 读 step3 的 WAVECAR 重启
    # ★★★ ISYM = 3 —— LHFCALC=.TRUE. 时 VASP 的默认值，必须显式写死 ★★★
    #   原来这里没写 ISYM，于是 build_incar() 会把 step3 的 ISYM 原样继承过来；
    #   step3 旧版是 ISYM=0，等于把"不做对称化"这个错误一路带进 HSE。
    #   ISYM=3 的含义（VASP Wiki）：不直接对称化电荷密度，而是把不可约区
    #   k 点上的轨道用对称操作旋转出来构造密度 —— 这正是 Fock 交换需要
    #   在整个 BZ 上求和时唯一正确的做法。
    #   注意 ISYM=2 与 ISYM=3 给出的不可约 k 点集相同，所以 step3(ISYM=2)
    #   的 WAVECAR 与本步(ISYM=3)完全兼容，NKPTS 不变。
    # ISYM / GGA 由主流程按 STEP4_ISYM / HYBRID_FLAVOR 注入
    "ALGO":     "Damped",     # HF 用 Damped/All；Normal/Fast 不适用
    "TIME":     "0.4",        # ALGO=Damped 的阻尼步长
    "LHFCALC":  ".TRUE.",     # 打开杂化
    "HFSCREEN": "0.11",       # ★ 屏蔽参数，权威值在此（0.2=标准 HSE06/HSEsol；0.3=HSE03）
    "AEXX":     "0.25",       # ★ 精确交换比例，权威值在此（0.25=标准 HSE06/HSEsol）
    "PRECFOCK": "Fast",       # 交换积分用粗档 FFT，HSE 主要提速点（定稿用 Normal 复核）
    "LORBIT":   "11",         # ★ 轨道投影：写 PROCAR/PROCAR_OPT（lm 分解权重）→ fat band-dft-cpu 必需
    "LWAVE":    ".TRUE.",     # ★ 存 WAVECAR（改自 .FALSE.）；因此 WAVECAR 一律拷贝、禁用符号链接（见 build_step4_dir）
    "LCHARG":   ".TRUE.",     # 存 CHGCAR
    "LASPH":    ".TRUE.",
    "LVHAR":    ".TRUE.",
}   # KPAR 由 auto_parallel() 注入；KPOINTS_OPT 下不写 NCORE/NPAR

# ---- 路径降采样默认值 ----
# 降采样是常态而非例外：HSE one-shot 成本 ∝ 路径点数，而出版级能带每腿 10~15 点
# 已经完全光滑。想用 step3 的完整路径请显式传 --line-density 0。
DEFAULT_LINE_DENSITY = 10

# ---- 并行自动设置 ----
KPAR_MAX     = 8            # KPAR 上限（Pb2Bi2Te5 SOC 96 核 KPAR=8 实测稳定；内存紧张调小）
MIN_KPTS_PER_GROUP = 4      # (已废弃，见 auto_parallel 的说明；保留仅为向后兼容)
TOTAL_CORES  = None         # None=从 submit.sh 自动读；也可手动写整数强制指定
# =================================================================


def parse_incar(path):
    """解析 INCAR 为有序 [(KEY, value), ...]，去掉注释与空行。"""
    items = []
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or s[0] in "#!":
                continue
            for c in ("#", "!"):
                if c in s:
                    s = s.split(c, 1)[0].strip()
            if "=" not in s:
                continue
            for part in s.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    items.append((k.strip().upper(), v.strip()))
    return items


def sanitize_label(text):
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return label.strip("_.-") or "material"


def read_structure_label(path):
    with open(path, encoding="utf-8-sig") as f:
        first = f.readline().strip()
    token = first.split()[0] if first.split() else "material"
    return sanitize_label(token)


def read_method_name(path):
    if not os.path.exists(path):
        return "unknown"
    for line in open(path, encoding="utf-8"):
        if line.startswith("FUNC="):
            return line.split("=", 1)[1].strip()
    return "unknown"


def detect_soc(items):
    """从 step3 INCAR 自动判定 SOC。LSORBIT=.TRUE. 表示 step3 产出的是旋量
    WAVECAR，step4 必须同样开 SOC 并走 vasp_ncl 才能热重启；反之走 vasp_std
    并剔除 SOC 标签。ISPIN=2（共线自旋极化）一律拒绝。"""
    data = {k: v for k, v in items}
    ispin = data.get("ISPIN", "").split()
    if ispin and ispin[0] == "2":
        raise SystemExit("[错误] step3 是 ISPIN=2（自旋极化）—— 本工作流不支持，请人工处理")
    val = data.get("LSORBIT", "").split()
    return bool(val) and val[0].upper().lstrip(".").startswith("T")


def read_kpoints_count(path):
    try:
        with open(path) as f:
            f.readline()
            return int(f.readline().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_nbands_from_outcar(path):
    """从 step3 OUTCAR 读并行版实际使用的 NBANDS（取最后一次出现，即 VASP 真正用的值）。"""
    if not os.path.exists(path):
        return None, "找不到 %s" % path
    val = None
    try:
        with open(path) as f:
            for line in f:
                if "NBANDS=" in line:
                    m = re.search(r"NBANDS=\s*(\d+)", line)
                    if m:
                        val = int(m.group(1))
    except OSError:
        return None, "读取 %s 失败" % path
    if val:
        return val, "读自 %s" % path
    return None, "%s 里没解析到 NBANDS" % path


def build_incar(src_items, remove, setd):
    """继承 src 键值，删除 remove，覆盖/新增 setd。SYSTEM 置顶。"""
    remove   = {k.upper() for k in remove}
    setd     = {k.upper(): v for k, v in setd.items()}
    src_keys = {k for k, _ in src_items}
    body, seen = [], set()
    for k, v in src_items:
        if k in remove or k == "SYSTEM":
            continue
        body.append((k, setd[k] if k in setd else v))
        seen.add(k)
    # ★ 修正: 同时出现在 remove 和 setd 里、且 step3 INCAR 也有的键会凭空消失
    #   （被 remove 跳过 -> 没进 seen -> 又因在 src_keys 里而不补写）。
    #   语义应当是 setd 永远赢。
    for k, v in setd.items():
        if k != "SYSTEM" and k not in seen:
            body.append((k, v))
    lines = []
    if "SYSTEM" in setd:
        lines.append("SYSTEM = %s" % setd["SYSTEM"])
    for k, v in body:
        lines.append("%-8s = %s" % (k, v))
    return "\n".join(lines) + "\n"


def render_submit(tpl_path, out_path, params):
    """从父目录 submit_ncl.tpl 渲染 step4/submit.sh。"""
    if not os.path.exists(tpl_path):
        raise SystemExit("[错误] 找不到提交模板 %s" % tpl_path)
    with open(tpl_path) as f:
        text = f.read()
    for key, val in params.items():
        text = text.replace("{{" + key + "}}", str(val))
    leftover = set(re.findall(r"\{\{(\w+)\}\}", text))
    if leftover:
        raise SystemExit("[错误] %s 仍有未填充占位符：%s（本脚本只填 JOBNAME，其余参数请直接固化在模板中）"
                         % (tpl_path, leftover))
    with open(out_path, "w") as f:
        f.write(text)
def effective_incar(src_items, remove, setd):
    """算出【最终真正会写进 INCAR】的键值字典，合并语义与 build_incar 完全一致。
       用来做依赖最终值的判断（比如 HFRCUT 要看最终有没有 HFSCREEN）。"""
    remove = {k.upper() for k in remove}
    setd = {k.upper(): v for k, v in setd.items()}
    eff = {}
    for k, v in src_items:
        if k in remove:
            continue
        eff[k] = setd.get(k, v)
    eff.update(setd)
    return eff


def _incar_true(val):
    return bool(val) and str(val).strip().upper().lstrip(".").startswith("T")


def _incar_float(val):
    try:
        return float(str(val).split()[0])
    except (ValueError, IndexError, AttributeError, TypeError):
        return None


def resolve_hfrcut(eff):
    """按 HFRCUT_MODE 决定要不要写 HFRCUT。返回 (值或 None, 说明)。"""
    if HFRCUT_MODE is None:
        return None, "HFRCUT_MODE=None —— 不写 HFRCUT（若是非屏蔽杂化请自行确认）"
    if HFRCUT_MODE != "auto":
        return str(HFRCUT_MODE), "HFRCUT=%s（HFRCUT_MODE 手动指定）" % HFRCUT_MODE
    if not _incar_true(eff.get("LHFCALC")):
        return None, "LHFCALC 未开启，HFRCUT 无意义"
    hfs = _incar_float(eff.get("HFSCREEN"))
    if hfs is not None and hfs > 0:
        return None, "屏蔽杂化 (HFSCREEN=%g)，长程 Fock 交换已截断，默认 HFRCUT=0 即可" % hfs
    if _incar_true(eff.get("LTHOMAS")):
        return None, "Thomas-Fermi 屏蔽 (LTHOMAS=.TRUE.)，默认 HFRCUT=0 即可"
    return "-1", ("★ 非屏蔽杂化（最终 INCAR 里没有 HFSCREEN>0 也没有 LTHOMAS）"
                  "配 KPOINTS_OPT 路径点 -> 已自动写入 HFRCUT=-1。"
                  "不加的话路径本征值会静默偏移，且不会有任何报错。")


LDAU_TAGS = ("LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ", "LDAUPRINT")


def build_hse_u(items, mode):
    """按 HSE_U_MODE 返回 (remove_list_extra, set_dict_extra, note)。
       remove: 需从继承里剔除的 LDAU 标签；set: 需注入/覆盖的 LDAU 行。"""
    has_u = any(k == "LDAU" for k, _ in items)
    if mode == "keep":
        if not has_u:
            return [], {}, "keep：step3 无 U，实际为纯 HSE06"
        return [], {}, "keep：保留 step3 继承的 U（HSE+U，请确认有文献依据）"
    if mode == "remove":
        return list(LDAU_TAGS), {}, ("remove：删除所有 LDAU（纯 HSE06）"
                                     if has_u else "remove：step3 本就无 U，纯 HSE06")
    if mode == "custom":
        # 从 POSCAR 元素顺序构造（用 step3 POTCAR 顺序更稳，这里用 items 里没有元素序，
        # 故要求调用方传入 symbols；简化：custom 用 HSE_U_VALUES 的键顺序）
        table = {k: float(v) for k, v in HSE_U_VALUES.items() if v not in (None, 0, 0.0)}
        if not table:
            return list(LDAU_TAGS), {}, "custom：HSE_U_VALUES 为空，退化为 remove（纯 HSE06）"
        # custom 需要按 POTCAR/POSCAR 元素顺序展开，交给调用处用 symbols 完成
        return ["__CUSTOM__"], table, "custom：使用 HSE 专用 U（见 HSE_U_VALUES）"
    raise SystemExit("[错误] HSE_U_MODE 只能是 remove/keep/custom，收到 %r" % mode)


def custom_u_lines(symbols, table):
    """custom 模式：按 POSCAR 元素顺序生成 LDAU* 行。"""
    order = list(dict.fromkeys(symbols or []))
    ldaul, ldauu, ldauj = [], [], []
    used = []
    for e in order:
        u = table.get(e)
        if u not in (None, 0, 0.0) and (e in HSE_F_ELEMS or e in HSE_D_ELEMS):
            l = 3 if e in HSE_F_ELEMS else 2
            ldaul.append(str(l)); ldauu.append("%g" % float(u)); ldauj.append("0.0")
            used.append("%s(U=%g,l=%d)" % (e, float(u), l))
        else:
            ldaul.append("-1"); ldauu.append("0.0"); ldauj.append("0.0")
    if not used:
        return None, "custom：无匹配的 d/f 元素，未加 U"
    lines = {
        "LDAU": ".TRUE.", "LDAUTYPE": "2",
        "LDAUL": " ".join(ldaul), "LDAUU": " ".join(ldauu),
        "LDAUJ": " ".join(ldauj), "LDAUPRINT": "1",
    }
    return lines, "custom：对 %s 加 U" % ", ".join(used)


def read_symbols_from_poscar(path):
    try:
        L = Path(path).read_text(encoding="utf-8-sig").splitlines()
        line6 = L[5].split()
        if line6 and line6[0].lstrip("-").isdigit():
            return None
        return line6
    except Exception:
        return None


def guess_total_cores(step4_dir):
    """从 submit.sh 猜总 MPI 核数：优先 mpirun -np/-n N，其次 SBATCH 节点×每节点任务数。"""
    if TOTAL_CORES:
        return int(TOTAL_CORES), "手动指定"
    path = os.path.join(step4_dir, "submit.sh")
    if not os.path.exists(path):
        return None, "没找到 %s/submit.sh" % step4_dir
    txt = open(path).read()
    m = re.search(r"(?:mpirun|mpiexec|srun)[^\n]*?\s-(?:np|n)\s+(\d+)", txt)
    if m:
        return int(m.group(1)), "mpirun -np"
    nodes = re.search(r"(?:--nodes|-N)[=\s]+(\d+)", txt)
    tpn   = re.search(r"ntasks-per-node[=\s]+(\d+)", txt)
    ntot  = re.search(r"(?:--ntasks|-n)[=\s]+(\d+)", txt)
    if nodes and tpn:
        return int(nodes.group(1)) * int(tpn.group(1)), "nodes×ntasks-per-node"
    if ntot:
        return int(ntot.group(1)), "ntasks"
    return None, "无法从 submit.sh 解析核数"


def resolve_nkpts_scf(step4_dir):
    """自洽网格的 k 点数。KPOINTS 若是自动网格（第 2 行为 0），
       就改读 step3 实跑产出的 IBZKPT —— 那才是 VASP 真正约化出来的点数。"""
    n = read_kpoints_count(os.path.join(step4_dir, "KPOINTS"))
    if n:
        return n, "KPOINTS 显式列表"
    n = read_kpoints_count(os.path.join(STEP3_DIR, IBZKPT_FILE))
    if n:
        return n, "%s/%s" % (STEP3_DIR, IBZKPT_FILE)
    return None, "未知（KPOINTS 是自动网格且找不到 step3 的 IBZKPT）"


def auto_parallel(step4_dir, kpar_override=None, nbands=None):
    """选一个合法的 KPAR。返回 (kpar, npar_group, nbands_adjusted, note)。

    KPOINTS_OPT 下 NCORE 恒为 1（不写任何 NCORE/NPAR），故组内 NPAR = 总核数/KPAR。
    约束：
      1) 总核数 % KPAR == 0
      2) KPAR <= 自洽网格 k 点数（超过只会让部分 k 组在自洽阶段闲着）
      3) NBANDS % (总核数/KPAR) == 0，否则 VASP 自行抬高 NBANDS，
         与 step3 WAVECAR 的能带数对不上，多出来的带会被随机初始化

    ★ 旧版用 `nkpts_scf // kpar >= MIN_KPTS_PER_GROUP(=4)` 做筛选，
      对 KPOINTS_OPT 工作流是错的：自洽网格常常只有几个不可约点
      （典型 6 个），该条件会把所有 kpar>1 全部否掉，最后退回 KPAR=1，
      等于完全放弃并行。而真正的大头是自洽之后那一轮几十上百个路径点的
      非自洽对角化，恰恰最吃 k 点并行。
    """
    cores, src = guess_total_cores(step4_dir)
    if not cores:
        return None, None, nbands, src
    nkpts_scf, nk_src = resolve_nkpts_scf(step4_dir)
    if kpar_override:
        if cores % kpar_override:
            raise SystemExit("[错误] --kpar %d 不能整除总核数 %d" % (kpar_override, cores))
        npar = cores // kpar_override
        nb = nbands
        if nb and nb % npar:
            raise SystemExit("[错误] --kpar %d 使组内 NPAR=%d，但 NBANDS=%d 不是它的倍数；"
                             "请换一个 KPAR 或手动调 NBANDS" % (kpar_override, npar, nb))
        return kpar_override, npar, nb, "%d 核（%s，--kpar 指定）" % (cores, src)

    cap = KPAR_MAX
    if nkpts_scf:
        cap = min(cap, nkpts_scf)
    cap = max(1, cap)
    # 两遍搜索：先找"KPAR 尽量大 且 NBANDS 正好能被组内 NPAR 整除"的解；
    # 找不到就退而求其次，只保证 KPAR 尽量大，再把 NBANDS 向上取整。
    # （不能反过来先保 NBANDS —— 那会在 96 核这种情况下把 KPAR 压到 1，
    #   变成 NPAR=96 的病态分解，正是旧版要避免的那种崩法。）
    divisors = [c for c in range(cap, 0, -1) if cores % c == 0]
    kpar = None
    for cand in divisors:
        if nbands and nbands % (cores // cand):
            continue
        kpar = cand
        break
    if kpar is None:
        kpar = divisors[0] if divisors else 1
    npar = cores // kpar
    nb = nbands
    if nb and nb % npar:
        nb = int(-(-nb // npar) * npar)      # 向上取整到 npar 的倍数
    return kpar, npar, nb, "%d 核（%s；自洽 k 点 %s ← %s）" % (
        cores, src, nkpts_scf if nkpts_scf else "未知", nk_src)


# ---------------------------------------------------------------------------
# KPOINTS_OPT 路径：降采样 + 切片
# ---------------------------------------------------------------------------
def load_kpath(step3_dir):
    """
    读 step3 的 kpath.json + KPOINTS_OPT（显式列表格式）。
    返回 (meta, header_lines[3], coord_lines[N])；两者按索引一一对应。
    """
    kj = Path(step3_dir) / KPATH_FILE
    ko = Path(step3_dir) / KPOPT_FILE
    if not ko.exists():
        raise SystemExit("[错误] 找不到 %s —— step3 必须已产出 KPOINTS_OPT" % ko)
    lines = ko.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    try:
        n = int(lines[1].split()[0])
    except (IndexError, ValueError):
        raise SystemExit("[错误] %s 不是显式列表格式（第 2 行应为点数）" % ko)
    header = lines[:3]
    coords = [ln for ln in lines[3:] if ln.split()][:n]
    if len(coords) != n:
        raise SystemExit("[错误] %s 声明 %d 个点，实际只有 %d 行坐标" % (ko, n, len(coords)))

    if kj.exists():
        meta = json.loads(kj.read_text(encoding="utf-8"))
    else:
        meta = {}
    pts = meta.get("kpoints") or []
    if pts and len(pts) != n:
        raise SystemExit("[错误] %s 有 %d 个点，但 %s 有 %d 个 —— step3 输出不一致"
                         % (KPATH_FILE, len(pts), KPOPT_FILE, n))
    if not pts:
        # 没有 kpath.json：从 KPOINTS_OPT 反解坐标，标签留空（不能降采样，只能切片）
        meta = {"kpoints": [[float(x) for x in ln.split()[:3]] for ln in coords],
                "point_labels": [""] * n, "breaks": [],
                "method": "reconstructed-from-KPOINTS_OPT",
                "note": "step3 缺 kpath.json，标签/断点信息缺失"}
    meta.setdefault("point_labels", [""] * n)
    meta.setdefault("breaks", [])
    return meta, header, coords


def leg_boundaries(meta):
    """腿的边界 = 有标签的高对称点 ∪ 段间跳变两侧 ∪ 首尾点。"""
    n = len(meta["kpoints"])
    labs = meta["point_labels"]
    breaks = sorted(set(int(b) for b in meta["breaks"]))
    bset = {0, n - 1}
    bset.update(i for i, l in enumerate(labs) if l)
    for j in breaks:
        bset.update((j - 1, j))
    anchors = sorted(i for i in bset if 0 <= i < n)
    return anchors, set(breaks)


def downsample_kpath(meta, coords, density):
    """
    每条腿（两个相邻边界点之间的连续段）保留 density 个点（含两端）。
    边界点（高对称点/跳变点/首尾）永远精确保留。
    返回 (new_meta, new_coords, kept_indices)；
    路径元数据不足以按腿切分时返回 (None, None, 原因字符串)，由调用方决定是否致命。
    """
    if density < 2:
        raise SystemExit("[错误] --line-density 至少为 2（腿的两端点），或用 0 表示不降采样")
    anchors, breaks = leg_boundaries(meta)
    if len(anchors) <= 2 and not any(meta["point_labels"]):
        return None, None, ("kpath.json 缺高对称点标签，无法按腿降采样"
                            "（step3 可能没写 kpath.json，或写的是旧格式）")
    keep = set(anchors)
    for a, b in zip(anchors, anchors[1:]):
        if b in breaks and b == a + 1:      # 段间跳变本身，无内部点
            continue
        m = b - a                            # 原始区间数
        npts = max(2, min(m + 1, density))
        keep.update(a + round(k * m / (npts - 1)) for k in range(npts))
    kept = sorted(keep)
    old2new = {o: i for i, o in enumerate(kept)}
    new_meta = dict(meta)
    new_meta["kpoints"]      = [meta["kpoints"][i] for i in kept]
    new_meta["point_labels"] = [meta["point_labels"][i] for i in kept]
    new_meta["breaks"]       = sorted(old2new[j] for j in breaks if j in old2new)
    new_meta["note"] = ((meta.get("note") or "") +
                        " | downsampled: %d -> %d pts (line-density=%d)"
                        % (len(coords), len(kept), density)).strip(" |")
    return new_meta, [coords[i] for i in kept], kept


def parse_slice(spec, n_total):
    """'2/3' -> [(2,3)]；'all/3' -> [(1,3),(2,3),(3,3)]；None -> [None]。"""
    if not spec:
        return [None]
    m = re.match(r"^(all|\d+)\s*/\s*(\d+)$", spec.strip(), re.IGNORECASE)
    if not m:
        raise SystemExit("[错误] --kpath-slice 格式应为 i/n 或 all/n，收到 %r" % spec)
    n = int(m.group(2))
    if n < 2:
        raise SystemExit("[错误] --kpath-slice 的 n 至少为 2")
    if n > n_total:
        raise SystemExit("[错误] 只有 %d 个路径点，切不了 %d 段" % (n_total, n))
    if m.group(1).lower() == "all":
        return [(i, n) for i in range(1, n + 1)]
    i = int(m.group(1))
    if not 1 <= i <= n:
        raise SystemExit("[错误] --kpath-slice %d/%d 越界" % (i, n))
    return [(i, n)]


def slice_bounds(n_total, n_parts):
    return [round(j * n_total / n_parts) for j in range(n_parts + 1)]


def write_kpoints_opt(out_dir, header, coord_subset, tag):
    hdr0 = (header[0].strip() or "k-path for band-dft-cpu structure") + tag
    hdr2 = header[2].strip() if len(header) > 2 and header[2].strip() else "Reciprocal"
    text = "\n".join([hdr0, str(len(coord_subset)), hdr2] + coord_subset) + "\n"
    (Path(out_dir) / KPOPT_FILE).write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 搭建单个 step4 目录（整条路径或其中一段）
# ---------------------------------------------------------------------------
def build_step4_dir(out_dir, args, ctx, part=None):
    """part=None 整条路径；part=(i,n) 第 i/n 段。返回打印用摘要。"""
    meta, header, coords = ctx["meta"], ctx["header"], ctx["coords"]
    n_total = len(coords)
    os.makedirs(out_dir, exist_ok=True)
    log, warn = [], []

    # --- 本段的路径点范围 ---
    if part:
        i, n = part
        b = slice_bounds(n_total, n)
        s, e = b[i - 1], b[i]
        tag = "  (part %d/%d: pts %d-%d of %d)" % (i, n, s + 1, e, n_total)
    else:
        s, e = 0, n_total
        tag = "  (%d pts)" % n_total

    # --- KPOINTS_OPT + kpath.json（完整路径元数据 + slice 字段）---
    write_kpoints_opt(out_dir, header, coords[s:e], tag)
    meta_out = dict(meta)
    if part:
        meta_out["slice"] = {"part": part[0], "n_parts": part[1],
                             "offset": s, "count": e - s, "total": n_total}
    else:
        meta_out.pop("slice", None)
    (Path(out_dir) / KPATH_FILE).write_text(
        json.dumps(meta_out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.append("%s (%d 点)%s + %s" % (KPOPT_FILE, e - s, "" if not part else "", KPATH_FILE))

    # --- 常规文件 ---
    for name in COPY_FILES:
        src = os.path.join(STEP3_DIR, name)
        if not os.path.exists(src):
            warn.append("找不到 %s/%s" % (STEP3_DIR, name))
            continue
        dst = os.path.join(out_dir, name)
        if name == "POSCAR":
            content = Path(src).read_text(encoding="utf-8-sig")
            with open(dst, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        else:
            shutil.copyfile(src, dst)
        log.append(name)

    # --- WAVECAR：拷贝（保护 step3 源）---
    # ★ 安全约束：本步 LWAVE=.TRUE. 会让 VASP 写自己的 WAVECAR。若此时用符号链接
    #   共享 step3 的源文件，写操作会【透过链接覆盖/损坏 step3 的源 WAVECAR】，
    #   多个切片目录并发写同一链接更会竞态损坏。故只要 LWAVE=.TRUE.，一律强制
    #   【拷贝】、忽略 --link-wavecar，以保护 step3 源。仅当 LWAVE=.FALSE.（本步
    #   不写 WAVECAR）时符号链接才安全。
    _lwave_true = _incar_true(INCAR_SET.get("LWAVE"))
    wsrc = Path(STEP3_DIR, "WAVECAR").resolve()
    wdst = Path(out_dir, "WAVECAR")
    if not wsrc.exists():
        warn.append("找不到 %s/WAVECAR —— step3 必须先跑完" % STEP3_DIR)
    else:
        size = wsrc.stat().st_size
        if size < 1024:
            warn.append("WAVECAR 只有 %d 字节，step3 可能还没跑完！" % size)
        if wdst.exists() or wdst.is_symlink():
            wdst.unlink()
        if args.link_wavecar and _lwave_true:
            warn.append("LWAVE=.TRUE. 与 --link-wavecar 冲突：符号链接会让本步写操作"
                        "覆盖 step3 的源 WAVECAR，已强制改为拷贝以保护源文件。")
        if args.link_wavecar and not _lwave_true:
            os.symlink(wsrc, wdst)
            log.append("WAVECAR -> 符号链接 %s (%.1f GB 共享；仅 LWAVE=.FALSE. 才安全)"
                       % (wsrc, size / 1e9))
        else:
            shutil.copyfile(wsrc, wdst)
            log.append("WAVECAR 拷贝 (%.1f MB)%s" % (
                size / 1e6,
                "（LWAVE=.TRUE. 强制拷贝，保护 step3 源）" if _lwave_true else ""))

    method_src = os.path.join(STEP3_DIR, METHOD_FILE)
    if os.path.exists(method_src):
        shutil.copyfile(method_src, os.path.join(out_dir, METHOD_FILE))
        log.append(METHOD_FILE)

    # --- submit.sh（先渲染，auto_parallel 要从里面读核数；模板按 SOC + 维度选）---
    label, method, soc = ctx["label"], ctx["method"], ctx["soc"]
    tpl  = ctx["submit_tpl"]
    vasp = "vasp_ncl" if soc else "vasp_std"
    jobname = args.jobname or sanitize_label("%s_s4HSE" % label)
    if part:
        jobname = "%s_p%dof%d" % (jobname, part[0], part[1])
    jobname = jobname[:80]
    render_submit(tpl, os.path.join(out_dir, "submit.sh"), {"JOBNAME": jobname})
    log.append("submit.sh  ← %s (%s, JOBNAME=%s)" % (tpl, vasp, jobname))

    # ---- 按总 k 点数（自洽不可约点 + 本目录能带点）决定 ntasks-per-node ----
    # 此时 out_dir 里 KPOINTS / KPOINTS_OPT 都已就位，可以定量判断。
    _override = dict(SUBMIT_OVERRIDE)
    _nkpts_scf, _nk_src = resolve_nkpts_scf(out_dir)
    _n_band = e - s                                  # 本目录实际的能带路径点数
    if _nkpts_scf is not None:
        _total_kpts = _nkpts_scf + _n_band
        if _total_kpts > KPTS_TOTAL_THRESHOLD:
            _override["ntasks_per_node"] = NTASKS_PER_NODE_LARGE
            log.append("总 k 点 %d（自洽 %d + 能带 %d）> %d → 覆盖 ntasks-per-node=%d"
                       % (_total_kpts, _nkpts_scf, _n_band,
                          KPTS_TOTAL_THRESHOLD, NTASKS_PER_NODE_LARGE))
        else:
            log.append("总 k 点 %d（自洽 %d + 能带 %d）≤ %d → ntasks-per-node 保持模板默认"
                       % (_total_kpts, _nkpts_scf, _n_band, KPTS_TOTAL_THRESHOLD))
    else:
        warn.append("无法确定自洽 k 点数（%s）—— 跳过 ntasks-per-node 自动放大，保持模板默认"
                    % _nk_src)

    _override.update(stepconf.read_submit(stepconf.CONF_NAME))
    _sub_changed = stepconf.apply_submit(os.path.join(out_dir, "submit.sh"), _override)
    if _sub_changed:
        log.append("submit.sh 覆盖 Slurm: %s" % ", ".join(_sub_changed))

    # --- INCAR ---
    items = ctx["items"]
    kpar, npar_grp, nb_par, par_src = auto_parallel(out_dir, args.kpar, ctx["nb_actual"])
    incar_set = dict(INCAR_SET)
    # ---- 杂化泛函族：决定半局域部分用 PE 还是 PS ----
    # flavor_mode 由 main() 解析（--hybrid-flavor 优先，否则退回 HYBRID_FLAVOR），
    # 经 ctx 传入，不再直接读模块级常量，以便命令行开头选档位。
    _flavor_mode = ctx.get("flavor_mode", HYBRID_FLAVOR)
    _up_gga = {k.upper(): v for k, v in items}.get("GGA", "PE").strip().upper()
    if _flavor_mode == "auto":
        _gga = "PS" if _up_gga.startswith("PS") else "PE"
    elif _flavor_mode == "hsesol":
        _gga = "PS"
    else:
        _gga = "PE"
    _flavor = "HSEsol" if _gga == "PS" else "HSE06"
    incar_set["GGA"] = _gga
    # ★ AEXX / HFSCREEN 直接沿用 INCAR_SET 的值（dict(INCAR_SET) 已带入），
    #   不再被模块级常量覆盖 —— 改屏蔽常数 / 精确交换比例请直接改 INCAR_SET 那两行。
    if _flavor_mode == "auto" and _up_gga.startswith("PS"):
        print("[..] 上游是 PBEsol，本步用 HSEsol（GGA=PS）保持泛函族一致。"
              "想与 HSE06 文献对比请传 --hybrid-flavor hse06。", file=sys.stderr)
    elif _flavor_mode == "hse06" and _up_gga.startswith("PS"):
        print("[注意] 上游是 PBEsol 但 --hybrid-flavor=hse06 强制 GGA=PE：几何来自 "
              "PBEsol、能带来自 PBE 基，两者不是同一泛函族（可与 HSE06 文献对比，"
              "但非自洽路线）。要泛函族一致请用 auto 或 hsesol。", file=sys.stderr)

    # ---- ISYM ----
    _isym4 = "3" if STEP4_ISYM == "auto" else str(STEP4_ISYM).strip()
    incar_set["ISYM"] = _isym4

    incar_set["SYSTEM"] = "%s %s%s band-dft-cpu (step4; geometry=%s)%s" % (
        label, _flavor, "+SOC" if soc else "", method, tag)
    if soc:
        incar_set["LSORBIT"]    = ".TRUE."
        incar_set["GGA_COMPAT"] = ".FALSE."
        incar_set["LMAXMIX"]    = "4"
        if "MAGMOM" not in {k for k, _ in items}:
            warn.append("step3 INCAR 里没有 MAGMOM —— step3 可能没按 SOC 规范跑，请核查")
    # ---- ISYM 一致性检查 ----
    # 若 step3 与本步的 ISYM 一个关一个开，两步的不可约 k 点集会不同，
    # ISTART=1 读 step3 的 WAVECAR 就会因 NKPTS 不匹配而失效（或被静默丢弃）。
    _step3_isym = {k: v for k, v in items}.get("ISYM", "").split()
    if _step3_isym:
        try:
            _v = int(_step3_isym[0])
        except ValueError:
            _v = None
        _v4 = int(_isym4)
        if _v is not None and (_v <= 0) != (_v4 <= 0):
            warn.append("step3 是 ISYM=%d、本步是 ISYM=%d —— 一个关对称一个开，"
                        "两步的不可约 k 点集不同，ISTART=1 读 step3 的 WAVECAR 会因 "
                        "NKPTS 不匹配而失效。请让两步的 ISYM 同为关(0)或同为开(2/3)："
                        "改 step3 的 STEP3_ISYM 或本步的 STEP4_ISYM。" % (_v, _v4))

    if kpar:
        incar_set["KPAR"] = str(kpar)
    # KPOINTS_OPT 驱动要求 NCORE=1：不写 NCORE/NPAR，并把 step3 继承来的删掉
    incar_remove = list(INCAR_REMOVE) + ["NCORE", "NPAR"]
    if STEP4_VDW == "remove":
        incar_remove += INCAR_REMOVE_VDW
    if not soc:
        incar_remove += INCAR_REMOVE_NOSOC   # 无 SOC：剔除旋量相关标签

    # ---- HSE 阶段 DFT+U 处理（三态：remove/keep/custom）----
    u_remove, u_set, u_note = build_hse_u(items, HSE_U_MODE)
    if u_remove == ["__CUSTOM__"]:
        symbols = read_symbols_from_poscar(os.path.join(out_dir, "POSCAR"))
        u_lines, u_note = custom_u_lines(symbols, u_set)
        incar_remove += list(LDAU_TAGS)          # 先清继承的，再注入自定义
        if u_lines:
            incar_set.update(u_lines)
    else:
        incar_remove += u_remove
    log.append("DFT+U: %s" % u_note)

    if ctx["nb_actual"]:
        nb_use = nb_par or ctx["nb_actual"]
        if nb_use != ctx["nb_actual"]:
            warn.append("NBANDS %d -> %d（对齐组内 NPAR=%s）。多出的空带会被随机初始化，"
                        "自洽后无影响，但若想完全复用 step3 的 WAVECAR，"
                        "可回 step3 把 NBANDS 也设成 %d 重跑。"
                        % (ctx["nb_actual"], nb_use, npar_grp, nb_use))
        incar_set["NBANDS"] = str(nb_use)

    # ---- 库仑奇点 (HFRCUT)：看【最终 INCAR】里到底有没有屏蔽 ----
    _hf_val, _hf_note = resolve_hfrcut(effective_incar(items, incar_remove, incar_set))
    if _hf_val is not None:
        incar_set["HFRCUT"] = _hf_val
    log.append("库仑奇点: %s" % _hf_note)

    ctx["flavor"], ctx["gga"], ctx["isym"] = _flavor, _gga, _isym4
    text = build_incar(items, incar_remove, incar_set)
    with open(os.path.join(out_dir, "INCAR"), "w") as f:
        f.write(text)

    return {"out_dir": out_dir, "range": (s, e), "jobname": jobname,
            "kpar": kpar, "npar_grp": npar_grp, "par_src": par_src,
            "log": log, "warn": warn, "soc": soc, "vasp": vasp}


def main():
    ap = argparse.ArgumentParser(description="从 step3 搭建 step4 HSE06 能带输入（KPOINTS_OPT；SOC 自动检测）")
    ap.add_argument("--line-density", type=int, default=None, metavar="N",
                    help="每条路径腿保留 N 个点（含两端高对称点）；默认 %d。"
                         "0 = 不降采样，用 step3 的完整路径。"
                         "HSE one-shot 成本 ∝ 路径点数，出版级能带 10~15 足够"
                         % DEFAULT_LINE_DENSITY)
    ap.add_argument("--kpath-slice", default=None, metavar="i/n",
                    help="把路径切成 n 段只生成第 i 段（step4_HSE_band_p{i}of{n}）；"
                         "all/n 一次生成全部段。各段并行提交，事后用 "
                         "gen_step4_Band_structure.py 自动拼回整条能带")
    ap.add_argument("--link-wavecar", action="store_true",
                    help="用符号链接共享 step3 WAVECAR（省磁盘）；仅 LWAVE=.FALSE. 时安全，"
                         "本版 LWAVE=.TRUE. 会自动忽略并改为拷贝，保护 step3 源")
    ap.add_argument("--kpar", type=int, default=None, help="手动指定 KPAR（默认自动）")
    ap.add_argument("--jobname", default=None, help="Slurm 作业名（默认 <label>_s4HSE）")
    ap.add_argument("--hybrid-flavor", choices=["auto", "hse06", "hsesol"], default=None,
                    metavar="MODE",
                    help="杂化泛函族：auto=跟随上游(PBEsol链→HSEsol, PBE链→HSE06)；"
                         "hse06=无论上游一律 GGA=PE；hsesol=无论上游一律 GGA=PS。"
                         "不传则沿用脚本内 HYBRID_FLAVOR（当前 %r）" % HYBRID_FLAVOR)
    args = ap.parse_args()

    # 命令行开关优先于脚本内常量，让"开头选档位"不必回来改文件。
    # 不传 --hybrid-flavor 时退回 HYBRID_FLAVOR，保持旧的默认行为。
    flavor_mode = args.hybrid_flavor or HYBRID_FLAVOR
    if flavor_mode not in ("auto", "hse06", "hsesol"):
        raise SystemExit("[错误] HYBRID_FLAVOR/--hybrid-flavor 只能是 auto/hse06/hsesol，收到 %r"
                         % flavor_mode)
    if args.hybrid_flavor:
        print("[..] 杂化泛函族：--hybrid-flavor %s（覆盖脚本内 HYBRID_FLAVOR=%r）"
              % (flavor_mode, HYBRID_FLAVOR), file=sys.stderr)

    if not os.path.isdir(STEP3_DIR):
        raise SystemExit("[错误] 找不到 %s —— 请在父目录下运行本脚本" % STEP3_DIR)
    incar_path = os.path.join(STEP3_DIR, INCAR_FILE)
    if not os.path.exists(incar_path):
        raise SystemExit("[错误] 找不到 %s" % incar_path)

    struct_src = os.path.join(STEP3_DIR, "POSCAR")
    label = read_structure_label(struct_src) if os.path.exists(struct_src) else "material"
    method = read_method_name(os.path.join(STEP3_DIR, METHOD_FILE))
    items = parse_incar(incar_path)
    soc = detect_soc(items)

    # ---- 维度：优先继承 step3 workflow_method.txt 的 DIM=，缺失按结构判定 ----
    dim, dim_note = resolve_dim(os.path.join(STEP3_DIR, METHOD_FILE), struct_src)
    require_dim(dim, ('2d', '3d'), "step4_hse",
                why="高对称路径定义在晶体倒空间，孤立分子没有能带")
    print("[..] 维度：%s — %s" % (dim.upper(), dim_note), file=sys.stderr)
    tpl_base = SUBMIT_TPL_NCL if soc else SUBMIT_TPL_STD
    tpl = str(resolve_tpl(Path.cwd(), tpl_base, dim))
    nb_actual, nb_src = read_nbands_from_outcar(os.path.join(STEP3_DIR, OUTCAR_FILE))

    # 路径：读 step3 -> 降采样（默认开启，--line-density 0 关闭）
    meta, header, coords = load_kpath(STEP3_DIR)
    n_orig = len(coords)
    explicit = args.line_density is not None
    density = args.line_density if explicit else DEFAULT_LINE_DENSITY
    ds_note = None
    if density:
        new_meta, new_coords, info = downsample_kpath(meta, coords, density)
        if new_meta is None:
            if explicit:
                raise SystemExit("[错误] %s —— 无法执行 --line-density %d；"
                                 "用 --line-density 0 跑完整路径" % (info, density))
            ds_note = "降采样已跳过（%s）；用 step3 完整路径" % info
            print("[注意] %s" % ds_note)
            density = 0
        else:
            meta, coords = new_meta, new_coords
    args.line_density = density   # 后续打印用解析后的实际值

    ctx = {"meta": meta, "header": header, "coords": coords,
           "label": label, "method": method, "items": items,
           "nb_actual": nb_actual, "soc": soc,
           "submit_tpl": tpl, "dim": dim,
           "flavor_mode": flavor_mode}

    parts = parse_slice(args.kpath_slice, len(coords))
    results = []
    for part in parts:
        out_dir = STEP4_DIR if part is None else "%s_p%dof%d" % (STEP4_DIR, part[0], part[1])
        results.append(build_step4_dir(out_dir, args, ctx, part))

    # ---------------- 汇总打印 ----------------
    print("体系/结构方法 : %s / %s" % (label, method))
    print("step4 泛函    : %s%s  (GGA=%s, AEXX=%s, HFSCREEN=%s, ISYM=%s, IVDW %s)"
          % (ctx.get("flavor", "?"), "+SOC" if soc else "", ctx.get("gga", "?"),
             INCAR_SET["AEXX"], INCAR_SET["HFSCREEN"], ctx.get("isym", "?"),
             "已删除" if STEP4_VDW == "remove" else "保留"))
    print("step4 二进制  : %s  [SOC 自动检测自 step3 LSORBIT]"
          % ("vasp_ncl" if soc else "vasp_std"))
    print("step4 维度    : %s（模板 %s；%s）"
          % (dim.upper(), os.path.basename(tpl), dim_note))
    print("输出文件      : LWAVE=.TRUE.（存 WAVECAR）  LORBIT=11（写 PROCAR/PROCAR_OPT，fat band-dft-cpu）"
          "  LCHARG=.TRUE.（存 CHGCAR）")
    print("WAVECAR 处理  : 一律拷贝 step3 源（LWAVE=.TRUE. 下 --link-wavecar 被忽略以防覆盖源）")
    print("能带路径      : step3 共 %d 点%s -> 本次使用 %d 点，切 %d 个目录"
          % (n_orig,
             "，降采样后 %d 点（每腿 %d%s）" % (len(coords), args.line_density,
                                          "" if explicit else "，默认值；--line-density 0 可关闭")
             if args.line_density else "（未降采样）",
             len(coords), len(results)))
    if nb_actual:
        print("NBANDS        : %d（对齐 step3 WAVECAR，%s）" % (nb_actual, nb_src))
    else:
        print("[注意] 没能从 step3 OUTCAR 读到 NBANDS（%s）—— 沿用 step3 INCAR 的值，"
              "若与 WAVECAR 不符会触发随机初始化" % nb_src)

    for r in results:
        s, e = r["range"]
        print("\n[%s]  路径点 %d-%d（%d 个）  JOBNAME=%s" %
              (r["out_dir"], s + 1, e, e - s, r["jobname"]))
        for x in r["log"]:
            print("   + %s" % x)
        if r["kpar"]:
            print("   并行: KPAR=%d, 组内 NPAR=%s, NCORE=1（KPOINTS_OPT 要求；不写 NCORE/NPAR）  [%s]"
                  % (r["kpar"], r["npar_grp"], r["par_src"]))
        else:
            print("   并行: 未自动设置（%s）—— 请手动在 INCAR 加 KPAR" % r["par_src"])
        for w in r["warn"]:
            print("   ! %s" % w)

    print("\n下一步:")
    print("   1. 检查各目录 submit.sh（应调用 %s）后逐个 sbatch" % results[0]["vasp"])
    print("   2. 每段各自重跑一次 SCF（Damped ~20 步，便宜），随后只算本段路径点")
    print("   3. 全部跑完后在父目录: python gen_step4_Band_structure.py"
          "  （自动发现 %s_p*of* 并拼接）" % STEP4_DIR)


if __name__ == "__main__":
    main()
