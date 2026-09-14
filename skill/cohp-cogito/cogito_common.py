#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cogito_common.py —— cohp-cogito 技能公共模块

职责：
  1. 定位上游 band-dft-cpu 的 step3_PBE_WAVECAR 目录并校验 COGITO 前置条件
  2. 把 COGITO 需要的 5 个文件拷进本步骤目录（POSCAR/POTCAR/OUTCAR/vasprun.xml/WAVECAR）
  3. 提供 conda 环境下调用 COGITO / COGITOanalyze / COGITOpost 的统一入口

COGITO 的前置条件（来自官方 tutorial，2026-09 实测）：
  NSW=0           静态计算
  ISYM in (1,2,3) 约化 k 网格 —— ISYM<=0 会导致 k 点重构失败
  LWAVE=.TRUE.    必须保存波函数
  NBANDS >= 12*natoms  （推荐 12~20 倍；9 原子取 144）
  LSORBIT 不存在   COGITO 不支持自旋轨道耦合
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import csv
from pathlib import Path

# COGITO 依赖的 5 个 VASP 文件
VASP_FILES = ["POSCAR", "POTCAR", "OUTCAR", "vasprun.xml", "WAVECAR"]

# jzzn 上的环境（可由 step.conf 覆盖）
DEFAULT_CONDA_SH = os.path.expanduser("~/miniconda3/etc/profile.d/conda.sh")
DEFAULT_CONDA_ENV = "atomate2_p_a"


def find_upstream_step3(start: Path | None = None) -> Path:
    """向上游寻找 band-dft-cpu 的 step3_PBE_WAVECAR 目录。

    搜索顺序：当前目录 -> 父目录 -> 祖父目录（tf 的 step 目录通常位于
    <work_dir>/<材料>/<技能>/stepN_xxx，上游在同一 <技能> 层）。
    """
    start = Path(start or os.getcwd()).resolve()
    names = ("step3_PBE_WAVECAR", "step3_wavecar")
    seen: list[Path] = []
    for base in [start, *start.parents]:
        for n in names:
            cand = base / n
            if cand.is_dir() and (cand / "WAVECAR").is_file():
                return cand
            seen.append(cand)
        # band-dft-cpu 子目录
        for sub in base.iterdir() if base.is_dir() else []:
            if sub.is_dir() and sub.name.startswith("band-dft-cpu"):
                for n in names:
                    cand = sub / n
                    if cand.is_dir() and (cand / "WAVECAR").is_file():
                        return cand
    raise SystemExit(
        "[错误] 找不到上游 step3_PBE_WAVECAR（含 WAVECAR）。\n"
        "       本技能需要 band-dft-cpu 的 step3 产物。已查找：\n  "
        + "\n  ".join(str(p) for p in seen[:8])
    )


def parse_incar(path: Path) -> dict:
    """解析 INCAR 为 {KEY: value}（值保留字符串）。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        line = line.split("!")[0].split("#")[0].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out


def preflight_check(step3: Path) -> dict:
    """校验 COGITO 的前置条件，返回信息字典；不满足则 SystemExit。"""
    incar = parse_incar(step3 / "INCAR")

    def gi(key, default=None):
        try:
            return int(re.sub(r"[^0-9+-]", "", incar.get(key, "")) or default)
        except Exception:
            return default

    nsw = gi("NSW", 0)
    isym = gi("ISYM", 2)
    nbands = gi("NBANDS")
    lwave = str(incar.get("LWAVE", "")).upper().strip(". ")
    lsorbit = "LSORBIT" in incar

    # 原子数（从 POSCAR 第 7 行读）
    nions = 0
    poscar = step3 / "POSCAR"
    if poscar.is_file():
        lines = poscar.read_text(errors="ignore").splitlines()
        if len(lines) > 6:
            try:
                nions = sum(int(x) for x in lines[6].split())
            except Exception:
                nions = 0

    info = {"NSW": nsw, "ISYM": isym, "NBANDS": nbands, "LWAVE": lwave,
            "LSORBIT": lsorbit, "NIONS": nions}
    errs: list[str] = []
    if nsw != 0:
        errs.append(f"NSW={nsw}（必须是 0，静态计算）")
    if isym is None or isym not in (1, 2, 3):
        errs.append(f"ISYM={isym}（COGITO 要求 1/2/3 约化网格；<=0 会导致 k 点重构失败）")
    if lwave not in ("TRUE", "T", ".TRUE."):
        errs.append(f"LWAVE={lwave!r}（必须 .TRUE.）")
    if lsorbit:
        errs.append("检测到 LSORBIT（COGITO 不支持 SOC）")
    if nbands and nions and nbands < 12 * nions:
        errs.append(
            f"NBANDS={nbands} < 12×NIONS={12 * nions}"
            "（COGITO 推荐 (12~20)×natoms）"
        )
    if errs:
        raise SystemExit(
            "[错误] 上游 step3 不满足 COGITO 前置条件：\n  - "
            + "\n  - ".join(errs)
            + f"\n  上游目录：{step3}"
        )
    return info


def stage_inputs(step3: Path, dest: Path) -> list[str]:
    """把 5 个 VASP 文件拷到 dest（硬拷贝，COGITO 不认软链接）。"""
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for f in VASP_FILES:
        src = step3 / f
        if not src.is_file():
            raise SystemExit(f"[错误] 上游缺少 {f}：{src}")
        shutil.copy2(src, dest / f)
        copied.append(f)
    return copied


def _conda_prefix(conda_sh: str, env: str) -> str:
    """生成在 bash 里激活 conda 环境的前缀命令。

    注意：conda_sh 必须先用 expanduser 展开 ~（shell 引号内的 ~ 不会展开），
    否则 source 会失败、conda 不激活、后续命令 "command not found"。
    """
    sh = os.path.expanduser(conda_sh)
    en = os.path.expanduser(env)
    return (
        f"source {sh!r} >/dev/null 2>&1 && "
        f"conda activate {en!r} >/dev/null 2>&1 && "
    )


def run_cogito_tool(tool: str, directory: Path, log: Path,
                    conda_sh: str = DEFAULT_CONDA_SH,
                    conda_env: str = DEFAULT_CONDA_ENV,
                    extra_args: list[str] | None = None,
                    timeout: int | None = None) -> int:
    """在 conda 环境下运行 COGITO / COGITOanalyze / COGITOpost。"""
    args = " ".join(extra_args or [])
    cmd = (
        _conda_prefix(conda_sh, conda_env)
        + f"{tool} --dir {str(directory)!r} {args}"
    )
    with log.open("w") as fh:
        fh.write(f"$ {cmd}\n\n")
        fh.flush()
        proc = subprocess.run(["bash", "-lc", cmd], stdout=fh,
                              stderr=subprocess.STDOUT, timeout=timeout)
    return proc.returncode


def load_stepconf_params(stepconf_mod=None) -> dict:
    """读本目录 step.conf 的 [params]（失败返回空 dict，绝不阻断流程）。"""
    try:
        if stepconf_mod is None:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import stepconf as stepconf_mod  # type: ignore
        conf = stepconf_mod.parse(
            Path(stepconf_mod.CONF_NAME).read_text(encoding="utf-8-sig"),
            stepconf_mod.CONF_NAME,
        )
        return {k.upper(): v for k, v, _ in conf.get("params", [])}
    except Exception:
        return {}


def read_quality(directory: Path) -> dict:
    """从 COGITO 的 error_output.txt 提取质量指标。"""
    out: dict = {}
    p = directory / "error_output.txt"
    if not p.is_file():
        return out
    text = p.read_text(errors="ignore")
    for key, pat in [
        ("charge_spilling_percent", r"percent charge spilling:\s*([0-9.eE+-]+)"),
        ("max_band_spill_percent", r"maximum band charge spill:\s*([0-9.eE+-]+)"),
        ("max_charge_spill_percent", r"maximum charge spill:\s*([0-9.eE+-]+)"),
        ("avg_orbital_mixing_percent", r"average orbital mixing:\s*([0-9.eE+-]+)"),
        ("max_orbital_mixing_percent", r"max orbital mixing:\s*([0-9.eE+-]+)"),
    ]:
        m = re.search(pat, text, re.I)
        if m:
            try:
                out[key] = float(m.group(1))
            except Exception:
                pass
    return out


def parse_bond_info(directory: Path) -> list[dict]:
    """解析 COGITOpost 的 bond_info.txt 为结构化列表。

    格式：Atom1 Atom2 dist(A) eV/bond elec/bond #/unit ev/unit
    """
    p = directory / "bond_info.txt"
    if not p.is_file():
        return []
    rows: list[dict] = []
    for line in p.read_text(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            a1, a2 = parts[0], parts[1]
            dist = float(parts[2]); ev = float(parts[3])
            elec = float(parts[4]); num = float(parts[5]); evu = float(parts[6])
        except Exception:
            continue
        if not (a1[0].isalpha() and a2[0].isalpha()):
            continue
        rows.append({"atom1": a1, "atom2": a2, "dist_ang": dist,
                     "icohp_ev_per_bond": ev, "icobi_elec_per_bond": elec,
                     "count_per_cell": num, "icohp_ev_per_cell": evu})
    return rows


def find_short_long(rows: list[dict], pair: tuple[str, str]) -> dict:
    """从键表中找 (pair) 的短键/长键（按距离升序取前两个不同距离）。"""
    a, b = pair
    cand = [r for r in rows
            if {r["atom1"], r["atom2"]} == {a, b} and r["icohp_ev_per_bond"] < 0]
    cand.sort(key=lambda r: r["dist_ang"])
    if len(cand) < 2:
        return {}
    return {"short": cand[0], "long": cand[1]}


def parse_cohp_html(directory: Path) -> list[dict]:
    path=directory / "bond_cohp_plot.html"
    if not path.is_file(): return []
    text=path.read_text(errors="ignore"); pos=text.find("Plotly.newPlot")
    if pos < 0: return []
    a=text.find("[",pos); depth=0; quote=False
    for i in range(a,len(text)):
        c=text[i]
        if c=="\"": quote=not quote
        if quote: continue
        if c=="[": depth+=1
        elif c=="]":
            depth-=1
            if depth==0:
                try: d=json.loads(text[a:i+1])
                except Exception: return []
                out=[]
                for t in d:
                    if not isinstance(t,dict): continue
                    vals=[]
                    for key in ("x","y"):
                        v=t.get(key)
                        if isinstance(v,dict) and "bdata" in v:
                            try:
                                raw=base64.b64decode(v["bdata"])
                                try: raw=zlib.decompress(raw)
                                except zlib.error: pass
                                v=list(__import__("numpy").frombuffer(raw,dtype=v.get("dtype","f8")))
                            except Exception: v=[]
                        vals.append(v)
                    if all(isinstance(v,list) for v in vals) and len(vals[0])==len(vals[1]): out.append({"name":str(t.get("name","")),"x":vals[0],"y":vals[1]})
                return out
    return []

def write_cohp_csv(traces: list[dict], path: Path) -> int:
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["trace","x","y"])
        for t in traces:
            for x,y in zip(t["x"],t["y"]): w.writerow([t["name"],x,y])
    return sum(len(t["x"]) for t in traces)
