# -*- coding: utf-8 -*-
"""klmace_common.py —— MACE 晶格热导率技能各步骤公共工具。

依赖：标准库 + dim_common（公共池 _common/opt/dim_common.py，tf 按 gen_need 推送）。
本文件只放轻量编排（维度/超胞/参数落盘/模板渲染/conda 子进程），保证登录节点的系统
python 也跑得动；重活（torch/mace/phono3py）一律在 conda 环境里由 mace_*.py 干。

与 kl_common.py 的差别：删掉全部 VASPKIT/POTCAR/ENCUT/GGA 相关内容（MACE 不需要），
conda 环境不再写死在本文件里，改由 step.conf 的 CONDA_SH / CONDA_ENV 提供——kl-dft-cpu 那版
"三处 conda 路径要手动保持一致"的坑在这里不复现。
"""
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import (detect_dimension, read_poscar_cell_frac,  # noqa: E402
                        validate_poscar, resolve_tpl, VACUUM_MIN,
                        _norm, _cross, _det3)

METHOD_FILE = "workflow_method.txt"   # step1 写 FUNC/DIM/MODEL，供后续步骤继承
KL_PARAMS = "klmace_params.txt"       # 跨步共享：DIM/SUPERCELL/FC2_SUPERCELL/MESH/METHOD

# conda 环境缺省值（仅作最后兜底：正常路径下 CONDA_SH/CONDA_ENV 由集群默认注入
# setting/<hpc>.yaml，或 step.conf 显式给出——见 _common/mace/README.md「换超算」）。
DEFAULT_CONDA_SH = "/public/home/wangchao/miniconda3/etc/profile.d/conda.sh"
DEFAULT_CONDA_ENV = "mace"


def env_src(conf=None):
    """拼出环境激活命令（非交互 shell 必须先 source）。

    CONDA_ENV 是 conda 环境名（如 `mace`）时走 `source conda.sh && conda activate <env>`；
    写成一个**路径**（含 `bin/activate`，如 `/path/to/venvs/mace_cpu`）时按 venv 激活——
    conda activate 激活不了普通 venv。留空走 DEFAULT_CONDA_ENV。
    """
    sh = env = None
    if conf is not None:
        try:
            sh, env = conf["CONDA_SH"], conf["CONDA_ENV"]
        except KeyError:
            pass
    sh = sh or DEFAULT_CONDA_SH
    env = env or DEFAULT_CONDA_ENV
    if env and "/" in env:
        venv = Path(os.path.expanduser(str(env))) / "bin" / "activate"
        if venv.is_file():
            return "source %s" % venv
    return "source %s && conda activate %s" % (sh, env)


# ==========================================================================
# 维度 / 方法继承
# ==========================================================================
def read_method(method_file):
    """读 workflow_method.txt → dict（键大写）。"""
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


def write_method(path, **kv):
    lines = ["%s=%s" % (k.upper(), v) for k, v in kv.items() if v is not None]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def resolve_dim(poscar, dimension="auto", vacuum_min=VACUUM_MIN):
    """返回 (dim, vac_axis)。dim ∈ {'0d','2d','3d'}；vac_axis 仅 2D 有意义。"""
    mode = str(dimension).lower()
    if mode in ("2d", "3d", "0d"):
        if mode != "2d":
            return mode, None
        _, axis, _ = detect_dimension(poscar, vacuum_min)
        return "2d", (axis if axis is not None else 2)
    dim, axis, _ = detect_dimension(poscar, vacuum_min)
    if dim == "2d" and axis != 2:
        sys.exit("[ERROR] 检测到 2D 但真空不在 c 轴（在第 %d 轴）。请把结构旋转成真空沿"
                 "第 3 个晶格矢量再重跑 step1。" % (axis + 1))
    return dim, (axis if dim == "2d" else None)


# ==========================================================================
# 超胞倍数 / phono3py --dim / --mesh 字符串（2D 真空方向恒 1）
# ==========================================================================
def supercell_matrix(poscar, dim, min_len=15.0, max_multiple=8, vac_axis=2,
                     cutoff=6.0):
    """对齐 kl-dft-cpu compute_supercell_reps 的双判据定对角超胞倍数 [na,nb,nc]。

    ① 边长判据（|a_i|×n ≥ min_len）：每条边模长 ≥ min_len，取 ceil(min_len/|a_i|)。
    ② 内切球判据（perp_i×n ≥ 分子投影范围 + 2×cutoff）：perp_i = V/|a_j×a_k| 是
       该方向的垂直胞高（最短周期距离，恒 ≤ |a_i|）。cutoff 取 MACE 截断半径 r_max
       （缺省 6.0 Å）：单分子/团簇原胞太小时，分子与其周期镜像会在截断半径内重叠，
       声子力常数被镜像的虚假相互作用污染、产生非物理虚频。故要求垂直胞高
       ≥ 分子在该方向的投影范围 + 2×cutoff，取 ceil((范围+2×cutoff)/perp_i)。
    两个判据取较大值。2D 真空方向恒 1。
    """
    lat, frac = read_poscar_cell_frac(poscar)
    lengths = [_norm(lat[i]) for i in range(3)]
    vol = abs(_det3(lat))
    perp = []
    for i in range(3):
        j, k = (i + 1) % 3, (i + 2) % 3
        area = _norm(_cross(lat[j], lat[k]))
        perp.append(vol / area if area > 1e-12 else 0.0)
    # 分子在每方向的笛卡尔投影范围（纯标准库，登录节点 python 也能跑）
    extent = [0.0, 0.0, 0.0]
    for i in range(3):
        lo = hi = sum(frac[0][k] * lat[k][i] for k in range(3))
        for n in range(1, len(frac)):
            c = sum(frac[n][k] * lat[k][i] for k in range(3))
            lo = min(lo, c)
            hi = max(hi, c)
        extent[i] = hi - lo
    reps = []
    for i in range(3):
        if dim == "2d" and i == (vac_axis if vac_axis is not None else 2):
            reps.append(1)
            continue
        n_len = max(1, int(math.ceil(min_len / max(lengths[i], 1e-6))))
        n_diam = max(1, int(math.ceil((extent[i] + 2.0 * cutoff) / max(perp[i], 1e-6))))
        n = max(n_len, n_diam)
        if n > max_multiple:
            print("[WARN] 方向 %d 需 %d 倍（边长 %.3f Å，垂直胞高 %.3f Å，"
                  "分子范围 %.3f Å），被 MAX_MULTIPLE=%d 截断 → 该方向实际仅 %.2f Å"
                  % (i, n, lengths[i], perp[i], extent[i], max_multiple,
                     max_multiple * perp[i]))
            n = max_multiple
        reps.append(n)
    return reps


def _det3(m):
    """3×3 整数矩阵行列式（纯标准库，超胞解析不依赖 numpy）。"""
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def is_matrix(spec):
    """3×3 嵌套（一般矩阵超胞）→ True；[n,n,n]（对角扩胞）→ False。"""
    return len(spec) == 3 and all(hasattr(r, "__len__") for r in spec)


def parse_reps(text, dim, vac_axis=2):
    """解析超胞规格，返回 [na,nb,nc]（对角）或 3×3 矩阵（一般扩胞）。

    两种写法与 hiphive / phonopy / phono3py 完全同义（照抄它们的语义）：
      · 3 个整数 —— 对角扩胞，等价 phonopy --dim="2 2 2"、ASE atoms.repeat((2,2,2))；
      · 9 个整数 —— 3×3 矩阵，**行主序**，等价 phonopy/phono3py --dim="2 1 0 -1 2 0 0 0 1"
                    （(a',b',c') = (a,b,c)·P，即 ASE make_supercell(atoms, P.T)）。
                    六方/菱形/单斜等"对角扩胞不经济"的体系用它。
    2D 材料强制真空方向不动：对角写法压 1；矩阵写法要求真空方向那行那列是单位基矢，
    否则直接报错（免得把真空和面内混一起，算出看似正常其实错的超胞）。
    """
    try:
        vals = [int(x) for x in str(text).split()]
    except ValueError:
        sys.exit("[ERROR] 超胞要写整数（3 个=对角扩胞，或 9 个=3×3 矩阵），收到 %r" % text)
    ax = 2 if vac_axis is None else int(vac_axis)
    if len(vals) == 3:
        if dim == "2d":
            vals[ax] = 1
        return vals
    if len(vals) != 9:
        sys.exit("[ERROR] 超胞要写 3 个（对角，如 \"3 3 3\"）或 9 个（3×3 矩阵，行主序，"
                 "如 \"2 1 0 -1 2 0 0 0 1\"）整数，收到 %r" % text)
    m = [vals[0:3], vals[3:6], vals[6:9]]
    det = _det3(m)
    if det == 0:
        sys.exit("[ERROR] 超胞矩阵行列式为 0（%s），不是合法扩胞矩阵" % m)
    if dim == "2d":
        for i in range(3):
            for j in range(3):
                if (i == ax or j == ax) and m[i][j] != (1 if i == j == ax else 0):
                    sys.exit("[ERROR] 2D 材料的超胞矩阵不能动真空方向（第 %d 轴）：%s\n"
                             "        真空方向那一行/列必须是 [0,0,1]，面内两轴随便混。"
                             % (ax + 1, m))
    return m


def sc_matrix(spec):
    """规格 → 3×3 嵌套列表，可直接喂 phonopy/phono3py 的 supercell_matrix。"""
    if is_matrix(spec):
        return [[int(x) for x in row] for row in spec]
    return [[int(spec[0]), 0, 0], [0, int(spec[1]), 0], [0, 0, int(spec[2])]]


def dim_str(reps):
    """规格 → 字符串：对角 "n n n"；一般矩阵 → 9 个整数（行主序，phono3py --dim 同义）。"""
    if is_matrix(reps):
        return " ".join(str(int(x)) for row in reps for x in row)
    return " ".join(str(int(x)) for x in reps)


def mesh_str(mesh, dim, vac_axis=2):
    m = [mesh, mesh, mesh] if isinstance(mesh, int) else [int(x) for x in mesh]
    if dim == "2d":
        m[vac_axis if vac_axis is not None else 2] = 1
    elif dim == "3d" and 1 in m:
        # 3D 材料：某方向网格=1 是 2D 模板残留，phono3py 会报 "Grid symmetry is
        # broken"。自动对齐到三个方向的最大值（各向同性），避免手写 2D 网格静默出错。
        n = max(m)
        if n > 1:
            m = [n, n, n]
    return " ".join(str(int(x)) for x in m)


def write_kl_params(path, **kv):
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
# conda 环境里跑命令
# ==========================================================================
def run_in_env(cmd_body, cwd, logname=None, conf=None):
    """在 conda 环境里跑一段命令（可含多条 && 串联），可 tee 日志。返回 returncode。"""
    tee = " 2>&1 | tee -a %s" % logname if logname else ""
    full = "%s && cd %s && ( %s )%s" % (env_src(conf), str(cwd), cmd_body, tee)
    return subprocess.run(["bash", "-lc", full]).returncode


def run_capture(cmd_body, cwd, conf=None):
    """同上但抓 stdout（用来向 conda 环境里的 python 问一句话）。-> (rc, stdout)。"""
    full = "%s && cd %s && ( %s )" % (env_src(conf), str(cwd), cmd_body)
    r = subprocess.run(["bash", "-lc", full], capture_output=True, text=True)
    return r.returncode, (r.stdout or "")


def run_phono3py(args_str, cwd, logname="phono3py.log", conf=None):
    print("[..] phono3py %s" % args_str)
    return run_in_env("phono3py %s" % args_str, cwd, logname, conf)


def check_env(conf, cwd="."):
    """开跑前先确认 conda 环境里 mace/phono3py 都在。失败直接退出，别等作业排到再炸。"""
    probe = ("python -c \"import phono3py,ase;print('[env] phono3py',phono3py.__version__)\" "
             "&& python -c \"import mace,torch;print('[env] mace',mace.__version__,"
             "'torch',torch.__version__,'cuda',torch.cuda.is_available())\"")
    if run_in_env(probe, cwd, None, conf) != 0:
        sys.exit("[ERROR] conda 环境 %s 里缺 phono3py / mace-torch / ase。\n"
                 "        改环境：tf -tt klmace -p <材料> -j <步骤> conf "
                 "--set params.CONDA_ENV=<你的环境名>" % (conf["CONDA_ENV"] or DEFAULT_CONDA_ENV))


# ==========================================================================
# 模板渲染 / 结构接力
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


def find_prev_dir(cwd, candidates):
    for name in candidates:
        d = Path(cwd) / name
        if (d / "CONTCAR").is_file() or (d / "POSCAR").is_file():
            return d
    return None


def link_or_copy(src, dst):
    """大文件（FORCES_FC3 可上 GB）优先软链，链不了再拷。"""
    dst = Path(dst)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.symlink(os.path.relpath(str(src), str(dst.parent)), str(dst))
    except OSError:
        shutil.copyfile(str(src), str(dst))


def new_jobname(cwd, step_label, tag="klm"):
    """作业名 <材料>-<tag>-<步骤>。tag 缺省 klm 保持 kl-mace 旧行为；
    opt-mace 传 tag="opt"，免得同一材料两个技能的 S1 作业在 squeue 里重名。"""
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
