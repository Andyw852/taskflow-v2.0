#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mlff_common.py —— mlff-mace（随机位移法 MLFF 训练）各步骤公共工具。

依赖：标准库 + dim_common（本技能目录内的副本）。
本文件只放轻量编排（维度/参数落盘/模板渲染/conda 子进程/停机守卫），保证登录节点
的系统 python 也跑得动；重活（torch/mace/phonopy/numpy）一律在 venv 环境里由
rattle_gen.py / fc2_calib.py / dataset_build.py / benchmark.py 等引擎干。

文件契约（全部在材料目录 <材料>/mlff-mace/ 下，材料目录 = 各 gen 脚本的 cwd）：
    workflow_method.txt      step1 弛豫引擎写的 FUNC/GGA/IVDW/DIM/MAG/LDAU
    mlff_params.txt          跨步共享的 key=value（DIM/SUPERCELL/RMAX/MODEL/GEN）
    convergence_history.json step8 逐代追加的验收曲线（停机规则读它）
    <步骤目录>/gen-<K>/      第 K 代产物（step4/6/8），顶层同名列一份“当前代”副本
                             （done_marker / 判据只看顶层同名文件）
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import (detect_dimension, read_poscar_cell_frac,  # noqa: E402
                        validate_poscar, resolve_tpl, VACUUM_MIN, _norm)

METHOD_FILE = "workflow_method.txt"
MLFF_PARAMS = "mlff_params.txt"
CONV_HISTORY = "convergence_history.json"

# conda 环境缺省值（可被 step.conf 的 CONDA_SH / CONDA_ENV 覆盖）
DEFAULT_CONDA_SH = "/public/home/wangchao/miniconda3/etc/profile.d/conda.sh"
DEFAULT_CONDA_ENV = "/public/home/wangchao/venvs/mace_cpu"


# ==========================================================================
# 环境
# ==========================================================================
def env_src(conf=None):
    """拼出环境激活命令。CONDA_ENV 含 '/' 时按 venv 激活，否则 conda activate。"""
    sh = env = None
    if conf is not None:
        try:
            sh, env = conf["CONDA_SH"], conf["CONDA_ENV"]
        except (KeyError, TypeError):
            pass
    sh = sh or DEFAULT_CONDA_SH
    env = env or DEFAULT_CONDA_ENV
    if env and "/" in str(env):
        venv = Path(os.path.expanduser(str(env))) / "bin" / "activate"
        if venv.is_file():
            return "source %s" % venv
    return "source %s && conda activate %s" % (sh, env)


def run_in_env(cmd_body, cwd, logname=None, conf=None):
    """在 venv 环境里跑一段命令，可 tee 日志。返回 returncode。"""
    tee = " 2>&1 | tee -a %s" % logname if logname else ""
    full = "%s && cd %s && ( %s )%s" % (env_src(conf), str(cwd), cmd_body, tee)
    return subprocess.run(["bash", "-lc", full]).returncode


def run_capture(cmd_body, cwd, conf=None):
    """同上但抓 stdout。-> (rc, stdout)。"""
    full = "%s && cd %s && ( %s )" % (env_src(conf), str(cwd), cmd_body)
    r = subprocess.run(["bash", "-lc", full], capture_output=True, encoding="utf-8")
    return r.returncode, (r.stdout or "")


def run_py(engine, args, cwd, conf=None, logname=None, script=None):
    """在 venv 里跑本技能的一个引擎脚本：python <engine> <args>。"""
    py = ("python %s" % engine) if engine.endswith(".py") else ("python %s" % engine)
    return run_in_env("%s %s" % (py, args), cwd, logname, conf)


def check_env(conf, cwd=".", want=("mace", "torch", "ase", "numpy", "phonopy")):
    """开跑前确认 venv 里关键包都在，失败直接退出（别等排到队再炸）。"""
    probe = ("python -c \"import %s\" 2>&1" % ",".join(want))
    rc, out = run_capture(probe, cwd, conf)
    if rc != 0:
        sys.exit("[ERROR] 环境 %s 里缺包：%s\n"
                 "        改环境：tf -tt mlff-mace -p <材料> -j <步骤> conf "
                 "--set params.CONDA_ENV=<venv路径或conda名>"
                 % (conf.get("CONDA_ENV") if conf else "?", out.strip().splitlines()[-1:]))


# ==========================================================================
# key=value 文件
# ==========================================================================
def read_kv(path):
    d = {}
    p = Path(path)
    if p.is_file():
        for ln in p.read_text(errors="ignore").splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                d[k.strip().upper()] = v.strip()
    return d


def write_kv(path, **kv):
    lines = ["%s=%s" % (k.upper(), v) for k, v in kv.items() if v is not None]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def read_json(path, default=None):
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8", newline="\n")


# ==========================================================================
# 维度
# ==========================================================================
def resolve_dim(poscar, dimension="auto", vacuum_min=VACUUM_MIN):
    """返回 (dim, vac_axis)。dim ∈ {'2d','3d'}；vac_axis 仅 2D 有意义（0/1/2）。"""
    mode = str(dimension).lower()
    if mode in ("2d", "3d"):
        if mode != "2d":
            return "2d" if mode == "2d" else "3d", None
        _, axis, _ = detect_dimension(poscar, vacuum_min)
        return "2d", (axis if axis is not None else 2)
    dim, axis, vacs = detect_dimension(poscar, vacuum_min)
    if dim == "0d":
        sys.exit("[ERROR] 检测到 0D 孤立体系（>=2 个真空方向）。mlff-mace 只支持 "
                 "2D/3D 周期晶体（训练要超胞与声子，孤立分子不适用）。")
    if dim == "2d" and axis != 2:
        sys.exit("[ERROR] 检测到 2D 但真空不在 c 轴（在第 %d 轴）。请把结构旋转成"
                 "真空沿第 3 个晶格矢量再重跑 step1。" % (axis + 1))
    return dim, (axis if dim == "2d" else None)


def supercell_reps_for(poscar, dim, rmax, min_atoms, max_atoms,
                       min_vacuum, vac_axis=2, max_try=12):
    """按「每方向胞长 ≥ 2·r_max 且原子数 ∈ [MIN_ATOMS, MAX_ATOMS]」定对角超胞倍数。

    2D：真空方向倍数恒 1，真空厚度 ≥ max(MIN_VACUUM, 2·r_max)（不足直接报错）；
    面内两方向按 2·r_max 判。3D：三方向都按 2·r_max 判。
    优先保证 2·r_max，原子数不足再逐方向 +1（先加最短边），超 MAX_ATOMS 报错。
    纯标准库。"""
    lat, frac = read_poscar_cell_frac(poscar)
    natom_prim = len(frac)
    lens = [_norm(lat[i]) for i in range(3)]
    reps = [1, 1, 1]
    target = 2.0 * rmax
    if dim == "2d":
        ax = vac_axis if vac_axis in (0, 1, 2) else 2
        vac = [v for v in _vacuum_per_axis_via_gap(lat, frac)]
        vac_thick = vac[ax]
        need = max(min_vacuum, target)
        if vac_thick < need:
            sys.exit("[ERROR] 2D 真空厚度 %.2f Å < max(MIN_VACUUM=%.2f, 2·r_max=%.2f) Å。"
                     "请先把真空层加到 %.1f Å 以上再重跑 step1。" %
                     (vac_thick, min_vacuum, target, need))
        reps[ax] = 1
        for i in range(3):
            if i == ax:
                continue
            reps[i] = max(1, int(math.ceil(target / max(lens[i], 1e-6))))
    else:
        for i in range(3):
            reps[i] = max(1, int(math.ceil(target / max(lens[i], 1e-6))))
    n = natom_prim * reps[0] * reps[1] * reps[2]
    for _ in range(max_try):
        if n >= min_atoms:
            break
        order = sorted(range(3), key=lambda i: lens[i] * reps[i])
        for i in order:
            if dim == "2d" and i == (vac_axis if vac_axis in (0, 1, 2) else 2):
                continue
            reps[i] += 1
            break
        n = natom_prim * reps[0] * reps[1] * reps[2]
    if n < min_atoms:
        sys.exit("[ERROR] 扩胞 %d 次后仍只有 %d 原子 < MIN_ATOMS=%d。" %
                 (max_try, n, min_atoms))
    if n > max_atoms:
        sys.exit("[ERROR] 满足 2·r_max=%.1f Å 的最小超胞 %s 有 %d 原子 > MAX_ATOMS=%d。"
                 "要么换 r_max 更小的基座模型，要么调 MIN_ATOMS/MAX_ATOMS。"
                 % (target, " ".join(map(str, reps)), n, max_atoms))
    return reps


def _vacuum_per_axis_via_gap(lat, frac):
    """与 dim_common.vacuum_per_axis 同法（避免重复实现内积）。"""
    from dim_common import vacuum_per_axis
    return vacuum_per_axis(lat, frac)


def dim_str(reps):
    return " ".join(str(int(x)) for x in reps)


def parse_reps(text, dim, vac_axis=2):
    reps = [int(x) for x in str(text).split()]
    if len(reps) != 3:
        sys.exit("[ERROR] 超胞倍数要写三个整数，收到 %r" % text)
    if dim == "2d":
        reps[vac_axis if vac_axis in (0, 1, 2) else 2] = 1
    return reps


# ==========================================================================
# 全局 step.conf 参数表（§11 全量参数）
# 同一份 templates/step.conf 经三层合并会带进每个步骤：step1 由 relax_common
# 声明容忍（见其 [MLFF] 补丁），step2~9 的 gen 脚本各自 SPEC = SHARED_PARAM_SPEC
# ∪ 本步专属键。改这里记得同步 README 的参数表。
# ==========================================================================
SHARED_PARAM_SPEC = {
    # --- 体系与维度 ---
    "FUNC": ("pbesol", "str"),               # pbe | pbesol | pbe-d3
    "DIMENSION": ("auto", "str"),            # auto | 2d | 3d（step1 里生效）
    "ENCUT_OVERRIDE": (None, "str"),         # 空 = 从 POTCAR 自动推 1.5×ENMAX
    "CELL_POLICY": (None, "str"),            # step1 弛豫取胞策略（relax_common）
    "VACUUM_AXIS_POLICY": (None, "str"),
    "AUTO_U": (None, "str"),                 # DFT+U：relax_common 阴离子门控逻辑
    "U_OVERRIDE": (None, "elemmap"),
    "U_ANION_GATE": (None, "bool"),
    "U_GATE_ANIONS": (None, "words"),
    # --- 数据模式 ---
    "DATA_MODE": ("scratch", "str"),         # scratch | extend
    "PRE_XYZ_FILES": (None, "str"),          # extend 用，逗号分隔
    "REF_FC2_PATH": (None, "str"),           # 有则跳过 DFT 声子基准
    # --- 结构生成 ---
    "VOL_FACTORS": ("0.97,1.00,1.03", "str"),  # 3D=体积因子；2D=面内晶格因子
    "RATTLE_STD": ("auto", "str"),           # auto = step3 自校准
    "RATTLE_STD_FALLBACK": ("0.03,0.06,0.10", "str"),
    "N_PER_CELL": (2, "int"),
    "MIN_DIST_RATIO": (0.75, "float"),
    "REF_DISP": (0.1, "float"),    # = autoplex 默认（0.01 Å 的位移力在损失里被 rattle 淹没 ~1000 倍，谐波刚度学不到；见 README 差异清单）
    "DISP_WARN": (80, "int"),
    "ISO_BOX": (15.0, "float"),
    "GRUNEISEN_STRAIN": (0.01, "float"),     # ±1% 晶格应变的 fc2
    "SEED_BASE": (2025, "int"),
    # --- 超胞 ---
    "MIN_ATOMS": (60, "int"),
    "MAX_ATOMS": (150, "int"),
    "MIN_VACUUM": (15.0, "float"),           # 2D
    # --- 迭代与停机 ---
    "GENERATION": (0, "int"),
    "MAX_GENERATION": (4, "int"),
    "RMS_MAX": (0.2, "float"),               # THz，主收敛闸
    "IMPROVE_MIN": (0.02, "float"),          # THz，判定本代是否有实质改善
    "GEN_INCREMENT": (20, "int"),
    "CURVE_POINTS": ("25,50,100,200,all", "str"),
    "CURVE_TOL": (0.05, "float"),
    "FORCE_CONTINUE": (False, "bool"),
    # --- 数据处理 ---
    "ENERGY_LIMIT": (0.005, "float"),        # eV/atom，离群过滤
    "FORCE_LIMIT": (40.0, "float"),          # eV/Å（=autoplex force_max 默认值，源码为准）
    "KSPACING_TOL": (0.20, "float"),
    # --- 微调 ---
    "MACE_MODEL": (None, "str"),             # 基座 .model 文件名/路径/基座名
    "MACE_MODEL_DIR": ("/public/home/wangchao/software/mace/mace_models", "str"),
    "REPLAY_XYZ": (None, "str"),
    "E0S_MODE": ("estimated", "str"),        # estimated | json（见 mace_finetune 注释）
    "N_COMMITTEE": (4, "int"),
    "ENERGY_WEIGHT": ("auto", "str"),        # 3D→1.0，2D→10.0
    "FORCES_WEIGHT": (100.0, "float"),
    "STRESS_WEIGHT": ("auto", "str"),        # 3D→10.0，2D→0
    "BATCH_SIZE": (10, "int"),           # autoplex MACE 默认 batch=10
    "DTYPE": ("float64", "str"),             # 不许改
    "LR": (1e-3, "float"),                # autoplex MACE 默认 1e-3（multihead 需 --force_mh_ft_lr）
    "EPOCHS": (1500, "int"),              # autoplex 上限 1500；PATIENCE 早停实际截断
    "PATIENCE": (100, "int"),             # 验证损失无改善连续多少代早停
    "START_SWA": (1200, "int"),           # autoplex SWA 起点
    "LOSS": ("huber", "str"),             # autoplex 用 huber
    "HUBER_DELTA": (0.05, "float"),       # [FIX-H2] MACE 默认 0.01 eV/Å 对本数据
                                          # 太小：力误差全在线性段=L1，推高 RMSE
    "USE_SWA": (False, "bool"),           # [FIX-H3] PATIENCE 早停会截断在
                                          # START_SWA 之前，默认不开
    "FORCE_MH_FT_LR": (True, "bool"),    # [FIX-LR] true=强制覆盖 MACE 多头微调 LR
                                          # （LR=1e-3 时 pt_head 发散，seed 间散布大）；
                                          # false=用 MACE 官方策略（诊断最优）
    "STRESS_WEIGHT_3D": (1.0, "float"),   # autoplex stress_weight=1.0
    "VALID_FRACTION": (0.10, "float"),
    "DEVICE": ("auto", "str"),
    "N_GPU": (0, "int"),   # GPU 微调时分卡的卡数：0=auto（=N_COMMITTEE 张，按 seed 均摊）；>0 显式卡数
    # --- 单点（step5）---
    "KPOINTS_GRID": (None, "str"),           # 空 = Γ-only；"2 2 2" 显式网格
    "EDIFF": (1e-7, "float"),
    "ALGO": ("Normal", "str"),
    "NCORE": (4, "int"),
    # --- 环境 ---
    "CONDA_SH": ("/public/home/wangchao/miniconda3/etc/profile.d/conda.sh", "str"),
    "CONDA_ENV": ("/public/home/wangchao/venvs/mace_cpu", "str"),
}


# ==========================================================================
# 模板 / 结构接力 / 作业名
# ==========================================================================
def render_tpl(tpl_path, subs, out_path):
    text = Path(tpl_path).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace("{{%s}}" % k, str(v))
    left = re.findall(r"\{\{([A-Z_0-9]+)\}\}", text)
    if left:
        sys.exit("[ERROR] 模板 %s 还有未填占位符：%s"
                 % (Path(tpl_path).name, ", ".join(sorted(set(left)))))
    Path(out_path).write_text(text, encoding="utf-8", newline="\n")
    print("[OK] %s" % Path(out_path).name)


def write_submit(tpl_path, out_path, subs):
    text = Path(tpl_path).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace("{{%s}}" % k, str(v))
    left = re.findall(r"\{\{([A-Z_0-9]+)\}\}", text)
    if left:
        sys.exit("[ERROR] 提交模板 %s 还有未填占位符：%s（模板改过、gen 脚本没跟上？）"
                 % (Path(tpl_path).name, ", ".join(sorted(set(left)))))
    Path(out_path).write_text(text, encoding="utf-8", newline="\n")
    os.chmod(str(out_path), 0o755)


def relay_poscar(prev_contcar, dst_poscar, label="上一步"):
    if not Path(prev_contcar).is_file():
        sys.exit("[ERROR] %s 的 CONTCAR 不存在：%s\n"
                 "        请确认上一步已完成再生成本步。" % (label, prev_contcar))
    bad = validate_poscar(prev_contcar)
    if bad:
        sys.exit("[ERROR] %s 的 CONTCAR 残缺（%s）：%s" % (label, bad, prev_contcar))
    shutil.copyfile(str(prev_contcar), str(dst_poscar))
    print("[OK] POSCAR ← %s" % prev_contcar)


def new_jobname(cwd, step_label, tag="mlff"):
    return "%s-%s-%s" % (Path(cwd).name, tag, step_label)


def resolve_submit(base_dir, kind="submit_mace", dim=None):
    """找提交模板：<kind>_<dim>.tpl → <kind>.tpl → resolve_tpl 兜底。"""
    base = Path(base_dir)
    names = ["%s.tpl" % kind]
    if dim:
        names.insert(0, "%s_%s.tpl" % (kind, dim))
    for name in names:
        if (base / name).is_file():
            return base / name
    try:
        return resolve_tpl(base, kind, dim or "3d")
    except SystemExit:
        sys.exit("[ERROR] 找不到提交模板 %s.tpl（gen_need 里列了吗？）" % kind)


# ==========================================================================
# 代数 / 停机守卫（gen 脚本用；判据不读 step.conf，改由 json 数据流驱动）
# ==========================================================================
def conv_history(path=CONV_HISTORY):
    """convergence_history.json → [记录, ...]（无则 []）。"""
    hist = read_json(path, default=[])
    return hist if isinstance(hist, list) else []


def conv_last_status(path=CONV_HISTORY):
    hist = conv_history(path)
    return (hist[-1].get("status") if hist else None), len(hist)


def halt_guard(conf, cwd="."):
    """§7.4 停机守卫：convergence_history.json 的 status 为 halt_* 时拒绝推进。
    FORCE_CONTINUE=true 显式放行。返回 (gen_now, gen_max)。"""
    gen = int(conf["GENERATION"])
    gmax = int(conf["MAX_GENERATION"])
    if gen > gmax:
        sys.exit("[ERROR] GENERATION=%d > MAX_GENERATION=%d —— 已到最大代数，硬停。"
                 "若确要继续，先调 MAX_GENERATION。" % (gen, gmax))
    status, n = conv_last_status(os.path.join(cwd, CONV_HISTORY))
    if status and str(status).startswith("halt_") and not conf["FORCE_CONTINUE"]:
        sys.exit("[ERROR] 检测到 %s（第 %d 条验收记录）：停机规则已生效，拒绝生成新数据。\n"
                 "         看 step8 的 validation_summary.json / convergence_history.json "
                 "排查清单；确认要继续加数据需显式设 FORCE_CONTINUE=true。"
                 % (status, n))
    return gen, gmax


def gen_dir_name(gen):
    return "gen-%d" % int(gen)


def current_gen_manifest(cwd, step4_dir="step4_genstruct"):
    """读当前代数：优先 step4 顶层 struct_manifest.json 的 generation 字段；
    缺失时回退 step.conf。-> int"""
    man = read_json(os.path.join(cwd, step4_dir, "struct_manifest.json"))
    if man and "generation" in man:
        return int(man["generation"])
    for name in sorted(Path(cwd).glob("step.conf")):
        for ln in Path(name).read_text(errors="ignore").splitlines():
            if ln.strip().upper().startswith("GENERATION"):
                try:
                    return int(ln.split("=", 1)[1].split("#")[0].strip())
                except ValueError:
                    pass
    return 0


# ==========================================================================
# 超胞结构工具（给 venv 引擎用；系统 python 无 numpy 时退化为慢速实现）
# ==========================================================================
def build_supercell(lat, frac, reps):
    """原胞 → 对角超胞 (cell, frac 坐标, 原子块顺序)。纯 python（慢但无依赖）。
    引擎里优先用 ase 的实现，此函数是兜底。原子顺序：image 优先（i,j,k 循环），
    与 ase repeat 一致 —— MAGMOM 展开按同一顺序。"""
    n = len(frac)
    ra, rb, rc = [int(x) for x in reps]
    cell = [[lat[i][j] * reps[i] for j in range(3)] for i in range(3)]
    out = []
    for i in range(ra):
        for j in range(rb):
            for k in range(rc):
                for a in range(n):
                    f = [(frac[a][0] + i) / ra, (frac[a][1] + j) / rb,
                         (frac[a][2] + k) / rc]
                    out.append(f)
    return cell, out
