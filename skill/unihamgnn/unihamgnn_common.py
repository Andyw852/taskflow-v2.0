# -*- coding: utf-8 -*-
"""unihamgnn_common.py —— Uni-HamGNN 能带技能公共工具（标准库 + dim_common）。

只放轻量编排（维度 / 路径校验 / 模板渲染 / submit 渲染 / conda 子进程），保证登录
节点系统 python 也跑得动；重活（torch / HamGNN / OpenMX）一律在 conda 环境里由
submit.sh 调用的驱动脚本干。

与 _common/mace/klmace_common.py 同一思路，按 Uni-HamGNN 的接口裁剪。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import detect_dimension  # noqa: E402

METHOD_FILE = "workflow_method.txt"

DEFAULT_CONDA_SH = "/public/home/wangchao/miniconda3/etc/profile.d/conda.sh"
DEFAULT_CONDA_ENV = "hamgnn"

# ---------------------------------------------------------------------------
# 全局 step.conf 参数声明（所有 gen 脚本共用同一份）。
# step.conf 是三层合并、每一步共用，脚本对 [params] 里没声明过的键直接 SystemExit
# —— 所以「只有某一步消费」的跨步骤键也要在这里统一声明（声明不等于使用）。
# ---------------------------------------------------------------------------
COMMON_SPEC = {
    # ---- 环境 ----
    "CONDA_SH": (DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (DEFAULT_CONDA_ENV, "str"),
    # ---- HamGNN / OpenMX 安装 ----
    "HAMGNN_DIR": ("", "str"),          # HamGNN 仓库目录（含 DFT_interfaces/ 与 Uni-HamGNN/）
    "UNI_MODEL": ("", "str"),           # Uni-HamGNN 通用模型 .pkl（Zenodo 下载）
    "OPENMX_POSTPROCESS": ("", "str"),  # 空 = $HAMGNN_DIR/DFT_interfaces/openmx/openmx_postprocess/openmx_postprocess
    "READ_OPENMX": ("", "str"),         # 空 = $HAMGNN_DIR/DFT_interfaces/openmx/read_openmx
    "DFT_DATA": ("", "str"),            # OpenMX DFT_DATA19 目录；空 = 用 OpenMX 默认 ../DFT_DATA19
    # ---- 维度 ----
    "DIMENSION": ("auto", "str"),       # auto | 2d | 3d
    # ---- 计算 ----
    "SOC": (True, "bool"),              # true = 通用 SOC 模型：non-SOC + SOC 两份 graph_data
    "NAO_MAX": (26, "int"),             # OpenMX 最大轨道数：14 / 19 / 26
    "NPROC": (24, "int"),               # openmx_postprocess 的 mpirun -np
    "MPIRUN": ("mpirun", "str"),         # mpirun 可执行；3090 上写 openmx_build 的完整路径
    "MKL_LIB": ("", "str"),            # openmx_postprocess 的 MKL .so.2 库目录（加进 LD_LIBRARY_PATH）；空 = 不额外设置
    "NTHREADS": (16, "int"),            # predict 的 OMP 线程数
    "DEVICE": ("cpu", "str"),           # predict 设备 cpu | cuda
    "NK": (120, "int"),                 # band_cal 能带路径 k 点数
    # ---- OpenMX SCF 参数（写进 poscar2openmx 的 basic_command）----
    "XC": ("GGA-PBE", "str"),
    "ENERGY_CUTOFF": (200.0, "float"),  # Ry
    "KGRID": ("5 5 5", "str"),
    "ELECTRONIC_TEMP": (300.0, "float"),# K
    "SCF_CRITERION": ("1.0e-7", "str"), # Hartree
    "MAX_SCF_ITER": (300, "int"),
}


def _get(conf, key, default=None):
    """从 StepConf 或 dict 取键；缺省返回 default（StepConf 没有 .get）。"""
    if conf is None:
        return default
    try:
        return conf[key]
    except (KeyError, TypeError):
        return default


def env_src(conf):
    """拼环境激活命令：CONDA_ENV 含 '/' 按 venv 激活，否则 conda activate。"""
    sh = _get(conf, "CONDA_SH") or DEFAULT_CONDA_SH
    env = _get(conf, "CONDA_ENV") or DEFAULT_CONDA_ENV
    if env and "/" in env:
        venv = Path(os.path.expanduser(str(env))) / "bin" / "activate"
        if venv.is_file():
            return "source %s" % venv
    return "source %s && conda activate %s" % (sh, env)


def render_tpl(tpl_path, subs, out_path):
    text = Path(tpl_path).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace("{{%s}}" % k, str(v))
    left = re.findall(r"\{\{([A-Z_0-9]+)\}\}", text)
    if left:
        sys.exit("[ERROR] 模板 %s 还有未填占位符：%s"
                 % (Path(tpl_path).name, ", ".join(sorted(set(left)))))
    Path(out_path).write_text(text, encoding="utf-8")


def write_submit(tpl_path, out_path, subs):
    text = Path(tpl_path).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace("{{%s}}" % k, str(v))
    left = re.findall(r"\{\{([A-Z_0-9]+)\}\}", text)
    if left:
        sys.exit("[ERROR] 提交模板 %s 还有未填占位符：%s"
                 % (Path(tpl_path).name, ", ".join(sorted(set(left)))))
    Path(out_path).write_text(text, encoding="utf-8")
    os.chmod(str(out_path), 0o755)


def write_method(path, **kv):
    lines = ["%s=%s" % (k.upper(), v) for k, v in kv.items() if v is not None]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_method(method_file):
    d = {}
    p = Path(method_file)
    if p.is_file():
        for ln in p.read_text(errors="ignore").splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                d[k.strip().upper()] = v.strip()
    return d


def resolve_dim(poscar, dimension="auto", vacuum_min=8.0):
    """返回 (dim, vac_axis)。dim ∈ {'0d','2d','3d'}；vac_axis 仅 2D 有意义。"""
    mode = str(dimension).lower()
    if mode in ("2d", "3d", "0d"):
        if mode != "2d":
            return mode, None
        _, axis, _ = detect_dimension(poscar, vacuum_min)
        return "2d", (axis if axis is not None else 2)
    dim, axis, _ = detect_dimension(poscar, vacuum_min)
    return dim, (axis if dim == "2d" else None)


def resolve_submit(base_dir, kind):
    p = Path(base_dir) / ("%s.tpl" % kind)
    if not p.is_file():
        sys.exit("[ERROR] 找不到提交模板 %s.tpl（gen_need 里列了吗？）" % kind)
    return p


def new_jobname(cwd, step_label, tag="uhg"):
    return "%s-%s-%s" % (Path(cwd).name, tag, step_label)


def resolve_bin(conf, key, fallback_rel):
    """返回可执行文件绝对路径：step.conf 显式值优先，否则 $HAMGNN_DIR/<fallback_rel>。"""
    v = _get(conf, key, "") or ""
    if v:
        return os.path.expanduser(str(v))
    hd = _get(conf, "HAMGNN_DIR", "") or ""
    if not hd:
        sys.exit("[ERROR] 未设置 %s 且 HAMGNN_DIR 也为空。\n"
                 "        请在 step.conf 设置：%s 或 HAMGNN_DIR（见 README）。"
                 % (key, key))
    return str(Path(os.path.expanduser(str(hd))) / fallback_rel)


def run_in_env(cmd_body, cwd, conf=None, logname=None):
    """在 conda 环境里跑一段命令（登录节点后处理用）。返回 returncode。

    pipefail 保证命令本身的退出码能穿透 tee 传回来（否则只能看到 tee 的 0）。
    """
    tee = " 2>&1 | tee -a %s" % logname if logname else ""
    full = "set -o pipefail; %s && cd %s && ( %s )%s" % (
        env_src(conf), str(cwd), cmd_body, tee)
    return subprocess.run(["bash", "-lc", full]).returncode
