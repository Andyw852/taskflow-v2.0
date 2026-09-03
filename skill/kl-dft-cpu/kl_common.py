# -*- coding: utf-8 -*-
"""kl_common.py —— 晶格热导率技能各步骤公共工具（step.conf 版）。

依赖：标准库 + dim_common（同目录）。重物理（ALM 定位移数、NAC/Born 提取、
拟合）由各 gen 脚本按需 import lattice_kappa（同目录，随 gen_need 推送）复用参考
引擎里已验证的函数，本文件只放轻量编排/字符串/子进程工具，保证登录节点系统
python 也能跑通编排部分。

放置：随每个用它的步骤 gen_need 推送到材料目录（skill 库里就一份在技能根，
per_step 布局下 find_asset 会从技能根回落取到）。
"""
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import (detect_dimension, force_kz1, read_poscar_cell_frac,  # noqa: E402
                        validate_poscar, resolve_tpl, VACUUM_MIN,
                        _norm, _cross, _det3)

METHOD_FILE = "workflow_method.txt"     # step1 写的 FUNC/GGA/DIM/MAG（继承泛函/维度）
KL_PARAMS   = "kl_params.txt"           # 本技能跨步共享：DIM/SUPERCELL/MESH/METHOD/NAC

# phono3py / alm 所在 conda 环境（按集群改；与 submit_*.tpl 保持一致）。
# 非交互 shell 里 conda activate 前必须 source conda.sh。
PHONO3PY_ENV_SRC = ("source /opt/miniconda3/etc/profile.d/conda.sh "
                    "&& conda activate atomate2_p_a")


# ==========================================================================
# 维度 / 泛函继承（读 step1 的 workflow_method.txt）
# ==========================================================================
def read_method(method_file):
    """读 step1 的 workflow_method.txt → dict（FUNC/GGA/IVDW/DIM/MAG 等，键大写）。"""
    d = {}
    p = Path(method_file)
    if not p.is_file():
        return d
    for ln in p.read_text(errors="ignore").splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            d[k.strip().upper()] = v.strip()
    return d


def read_method_dim(method_file):
    v = read_method(method_file).get("DIM", "").lower()
    return v if v in ("2d", "3d") else None


def resolve_dim(poscar, dimension="auto", vacuum_min=VACUUM_MIN):
    """返回 (dim, vac_axis)。dim ∈ {'2d','3d'}；vac_axis 仅 2D 有意义。"""
    mode = str(dimension).lower()
    if mode in ("2d", "3d"):
        return mode, (2 if mode == "2d" else None)
    dim, axis, _ = detect_dimension(poscar, vacuum_min)
    if dim == "2d" and axis != 2:
        sys.exit("[ERROR] 检测到 2D 但真空不在 c 轴（在 %d 轴）。请把结构旋转成"
                 "真空沿第 3 个晶格矢量再重跑（step1 会自动轮换，从 step1 重跑即可）。" % axis)
    return dim, (axis if dim == "2d" else None)


GGA_MAP = {"pbe": "PE", "pbesol": "PS", "pbe-d3": "PE"}
VDW_MAP = {"pbe": None, "pbesol": None, "pbe-d3": "12"}


# ==========================================================================
# 超胞倍数 / phono3py --dim / --mesh 字符串（2D 真空方向恒 1）
# ==========================================================================
def _supercell_geometry(poscar, reps):
    """超胞几何（纯标准库）：返回 (lengths, perp, insphere)。
    lengths = 三条边模长；perp = 每方向垂直胞高 V/|a_j×a_k|（周期性最短镜像距离，
    恒 ≤ 对应边长）；insphere = min(perp) = 内切球直径。"""
    lat, _ = read_poscar_cell_frac(poscar)
    sc = [[reps[i] * lat[i][k] for k in range(3)] for i in range(3)]
    lengths = [_norm(sc[i]) for i in range(3)]
    vol = abs(_det3(sc))
    perp = []
    for i in range(3):
        j, k = (i + 1) % 3, (i + 2) % 3
        area = _norm(_cross(sc[j], sc[k]))
        perp.append(vol / area if area > 1e-12 else 0.0)
    return lengths, perp, min(perp)


def warn_cutoff_vs_supercell(poscar, reps, cutoff, label="cutoff", margin=0.1):
    """截断半径必须 ≤ 超胞安全截断（0.5×内切球直径 − margin），否则周期性镜像让
    力常数对重复计数（参考 lattice_kappa._max_safe_cutoff）。只 WARN 不拦截。
    返回安全截断 (Å)；cutoff 为 None 时返回 None。"""
    if cutoff is None:
        return None
    _, _, insphere = _supercell_geometry(poscar, reps)
    max_safe = 0.5 * insphere - margin
    if float(cutoff) > max_safe:
        print("[WARN] %s=%.2f Å > 超胞安全截断 %.2f Å（内切球直径 %.2f Å）——"
              "周期镜像会污染力常数！建议 %s ≤ %.2f Å，或扩超胞（MIN_SC_LEN/SUPERCELL）。"
              % (label, float(cutoff), max_safe, insphere, label, max_safe))
    return max_safe


def supercell_matrix(poscar, dim, min_len=15.0, max_multiple=6, vac_axis=2,
                     cutoff=None, margin=0.1):
    """按"每个非真空方向胞长 ≥ min_len"定对角超胞倍数 [na,nb,nc]。
    2D 真空方向恒 1。
    cutoff 给定时（三阶/二阶截断半径 Å）再加内切球判据：垂直胞高 ≥ 2×(cutoff+margin)
    （垂直胞高是周期性最短镜像距离，必须 ≥ 2×截断半径，否则镜像虚假相互作用污染力常数）。
    被 max_multiple 截断时只 WARN 不拦截（对齐 reference engine validate_user_supercell）。
    纯标准库（读 POSCAR 晶格），不依赖 ASE。"""
    lat, _ = read_poscar_cell_frac(poscar)
    lens = [_norm(lat[i]) for i in range(3)]
    vol = abs(_det3(lat))
    perp = []
    for i in range(3):
        j, k = (i + 1) % 3, (i + 2) % 3
        area = _norm(_cross(lat[j], lat[k]))
        perp.append(vol / area if area > 1e-12 else 0.0)
    need = 2.0 * float(cutoff) + 2.0 * float(margin) if cutoff is not None else None
    reps = []
    for i in range(3):
        if dim == "2d" and i == (vac_axis if vac_axis is not None else 2):
            reps.append(1)
            continue
        n = max(1, int(math.ceil(min_len / max(lens[i], 1e-6))))
        if need is not None:
            n = max(n, int(math.ceil(need / max(perp[i], 1e-6))))
        if n > max_multiple:
            print("[WARN] 方向 %d 需 %d 倍（边长 %.3f Å，垂直胞高 %.3f Å%s），"
                  "被 MAX_MULTIPLE=%d 截断 → 该方向实际仅 %.2f Å"
                  % (i, n, lens[i], perp[i],
                     "，截断 %.2f Å 要求垂直胞高 ≥ %.2f Å" % (float(cutoff), need)
                     if need is not None else "",
                     max_multiple, max_multiple * perp[i]))
            n = max_multiple
        reps.append(n)
    return reps


def dim_str(reps):
    return " ".join(str(int(x)) for x in reps)


def mesh_str(mesh, dim, vac_axis=2):
    m = [mesh, mesh, mesh] if isinstance(mesh, int) else [int(x) for x in mesh]
    if dim == "2d":
        m[vac_axis if vac_axis is not None else 2] = 1
    return " ".join(str(int(x)) for x in m)


def write_kl_params(path, **kv):
    """把 DIM/SUPERCELL/MESH/METHOD/NAC 等落盘，供后续步骤严格继承。"""
    lines = ["%s=%s" % (k.upper(), v) for k, v in kv.items() if v is not None]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def read_kl_params(path):
    d = {}
    p = Path(path)
    if p.is_file():
        for ln in p.read_text(errors="ignore").splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                d[k.strip().upper()] = v.strip()
    return d


# ==========================================================================
# phono3py / 通用 conda-env 命令
# ==========================================================================
def run_in_env(cmd_body, cwd, logname=None, env_src=None):
    """在 conda 环境里跑一段命令（可含多条 && 串联），可 tee 日志。返回 returncode。"""
    env = env_src if env_src is not None else PHONO3PY_ENV_SRC
    tee = " 2>&1 | tee -a %s" % logname if logname else ""
    full = "%s && cd %s && ( %s )%s" % (env, str(cwd), cmd_body, tee)
    return subprocess.run(["bash", "-lc", full]).returncode


def run_phono3py(args_str, cwd, logname="phono3py.log", env_src=None):
    print("[..] phono3py %s" % args_str)
    return run_in_env("phono3py %s" % args_str, cwd, logname, env_src)


# ==========================================================================
# VASPKIT：KPOINTS / POTCAR / ENCUT
# ==========================================================================
def vaspkit_kpoints(outdir, kscheme="2", kspacing="0.04", exe="vaspkit",
                    dim="3d", vac_axis=2, force_kz1_2d=True):
    print("[..] VASPKIT KPOINTS：1 -> 102 -> %s -> %s" % (kscheme, kspacing))
    subprocess.run([exe], input="1\n102\n%s\n%s\n" % (kscheme, kspacing),
                   text=True, cwd=outdir, check=True)
    if dim == "2d" and force_kz1_2d:
        changed, note = force_kz1(Path(outdir) / "KPOINTS",
                                  axis=vac_axis if vac_axis is not None else 2)
        print("[%s] 2D KPOINTS 真空方向细分：%s" % ("OK" if changed else "..", note))


def vaspkit_potcar(outdir, exe="vaspkit"):
    if (Path(outdir) / "POTCAR").exists():
        return
    print("[..] VASPKIT POTCAR：1 -> 103")
    subprocess.run([exe], input="1\n103\n", text=True, cwd=outdir, check=True)


def encut_from_potcar(potcar, factor=1.5, fallback=400):
    vals = []
    for ln in Path(potcar).read_text(errors="ignore").splitlines():
        m = re.search(r"ENMAX\s*=\s*([\d.]+)", ln)
        if m:
            vals.append(float(m.group(1)))
    if not vals:
        return int(fallback)
    return int(math.ceil(max(vals) * factor / 10.0) * 10)


# ==========================================================================
# 模板渲染 / 提交 / 结构接力
# ==========================================================================
def render_tpl(tpl_path, subs, out_path):
    text = Path(tpl_path).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace("{{%s}}" % k, str(v))
    left = re.findall(r"\{\{([A-Z_]+)\}\}", text)
    if left:
        sys.exit("[ERROR] 模板 %s 还有未填占位符：%s"
                 % (Path(tpl_path).name, ", ".join(sorted(set(left)))))
    Path(out_path).write_text(text, encoding="utf-8", newline="\n")
    print("[OK] %s" % Path(out_path).name)


def _strip_doc_placeholders(text):
    """把说明注释里的 {{X}} 中和成 X。

    kls6：模板头部的"占位符 {{JOBNAME}} {{P3PY_CMD}}"这类说明行也会被
    全局 replace 命中，而 P3PY_CMD 是多行命令块 —— 一旦塞进注释行，它就排在
    所有 #SBATCH 之前，SLURM 遇到第一条可执行语句即停止解析指令，partition/
    ntasks/qos/output 全部失效（作业按默认队列跑，且后处理先于主程序执行）。
    真正的占位符写在 #SBATCH 行或正文里，不受影响。
    """
    out = []
    for ln in text.split("\n"):
        s = ln.lstrip()
        if s.startswith("#") and not s.startswith("#SBATCH") and "{{" in ln:
            ln = re.sub(r"\{\{(\w+)\}\}", r"\1", ln)
        out.append(ln)
    return "\n".join(out)


def _check_sbatch_order(path):
    """#SBATCH 必须全部位于第一条可执行语句之前，否则 SLURM 静默忽略。"""
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    first_cmd = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        first_cmd = i
        break
    if first_cmd is None:
        return
    bad = [i + 1 for i, ln in enumerate(lines[first_cmd + 1:], start=first_cmd + 1)
           if ln.strip().startswith("#SBATCH")]
    if bad:
        sys.exit("[ERROR] %s 第 %s 行的 #SBATCH 排在可执行语句(第 %d 行)之后，"
                 "SLURM 会全部忽略它们（分区/核数/时限/输出都会失效）。"
                 "多半是多行占位符被填进了头部注释——检查模板。"
                 % (Path(path).name, ",".join(str(b) for b in bad), first_cmd + 1))


def write_submit(tpl_path, out_path, subs):
    text = _strip_doc_placeholders(Path(tpl_path).read_text(encoding="utf-8"))
    for k, v in subs.items():
        text = text.replace("{{%s}}" % k, str(v))
    Path(out_path).write_text(text, encoding="utf-8", newline="\n")
    _check_sbatch_order(out_path)
def relay_poscar(prev_contcar, dst_poscar, label="上一步"):
    if not Path(prev_contcar).is_file():
        sys.exit("[ERROR] %s 的 CONTCAR 不存在：%s\n"
                 "        请确认上一步已完成再生成本步。" % (label, prev_contcar))
    bad = validate_poscar(prev_contcar)
    if bad:
        sys.exit("[ERROR] %s 的 CONTCAR 残缺（%s）：%s" % (label, bad, prev_contcar))
    shutil.copyfile(prev_contcar, dst_poscar)
    print("[OK] POSCAR ← %s" % prev_contcar)


def find_prev_dir(cwd, candidates):
    for name in candidates:
        d = Path(cwd) / name
        if (d / "CONTCAR").is_file() or (d / "POSCAR").is_file():
            return d
    return None


def new_jobname(cwd, step_label):
    return "%s-kl-dft-cpu-%s" % (Path(cwd).name, step_label)


def resolve_submit(base_dir, dim, kind="submit_std"):
    """按维度找提交模板：<kind>_<dim>.tpl，回退 <kind>.tpl / resolve_tpl。"""
    base = Path(base_dir)
    for name in ("%s_%s.tpl" % (kind, dim), "%s.tpl" % kind):
        if (base / name).is_file():
            return base / name
    try:
        return resolve_tpl(base, kind, dim)
    except SystemExit:
        sys.exit("[ERROR] 找不到 %s 的提交模板（%s_%s.tpl）" % (dim.upper(), kind, dim))
