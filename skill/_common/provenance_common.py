#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""provenance_common.py —— 给计算脚本/作业用的 provenance 补写器（公共池）

分工（谁写什么）：
  · tf 自己在 gen 推送阶段写 <材料>/provenance/<步骤>.json —— 输入文件 sha256、
    step.conf 合并后的参数、hpc/远端路径、生成器脚本 sha256。**自动、全技能覆盖**。
  · 本模块由 gen 脚本或作业脚本**可选**调用，补上 tf 从外面看不到的东西：
    工具版本（VASP/MACE/phono3py/Pheasy…）、产物 sha256、作业号、墙钟、退出码，
    并把结果写进**本步骤目录**的 provenance.json（随 fetch 回拉本地，供
    `tf prove -p 材料` 阅读）。

用法（脚本里三行，任何异常都不该让计算失败）：

    try:
        import provenance_common as P
    except ImportError:
        P = None
    ...
    if P:
        P.record(step="step2_static", skill="kl-dft-cpu",
                 inputs=["POSCAR", "INCAR", "KPOINTS", "POTCAR", "submit.sh"],
                 outputs=["OUTCAR", "vasprun.xml"],
                 tools=["vasp", "vaspkit"], note="静态自洽")

命令行自测（在任意步骤目录里）：
    python3 provenance_common.py --step step2_static --inputs POSCAR INCAR \
            --outputs OUTCAR --tools vasp phono3py
    python3 provenance_common.py --show            # 看当前目录的档案

纪律：只加字段、不删字段；写失败只打印一行警告，绝不抛异常、绝不 exit 非零。
"""
import datetime
import json
import os
import socket
import subprocess
import sys

PROV_NAME = "provenance.json"

# 名字 → pip 包名（用于 importlib.metadata 取版本）
_PY_PKGS = {
    "mace": "mace-torch", "mace-torch": "mace-torch", "torch": "torch",
    "ase": "ase", "phono3py": "phono3py", "phonopy": "phonopy",
    "symfc": "symfc", "pheasy": "pheasy", "spglib": "spglib",
    "numpy": "numpy", "hiphive": "hiphive", "jarvis-tools": "jarvis-tools",
}
# 走命令行取版本的可执行（vasp 没有 --version，单独处理）
_EXES = {"phono3py": "phono3py", "phonopy": "phonopy", "pheasy": "pheasy",
         "vaspkit": "vaspkit", "shengbte": "ShengBTE", "vasp_std": "vasp_std"}


def _sha256(path):
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                b = fh.read(1 << 20)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except OSError:
        return None


def _file_entry(path):
    if not os.path.isfile(path):
        return {"missing": True}
    st = os.stat(path)
    return {"sha256": _sha256(path), "bytes": st.st_size,
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}


def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr or "").strip()
    except Exception:            # noqa: BLE001（找不到/超时/权限都算"没查到"）
        return ""


def _vasp_version():
    """VASP 版本只能从 OUTCAR 头里读（vasp_std --version 不可靠）。"""
    import glob
    cands = ["OUTCAR"] + sorted(glob.glob(os.path.join("..", "*", "OUTCAR")))[:4]
    for p in cands:
        try:
            with open(p, errors="ignore") as fh:
                for _ in range(200):
                    line = fh.readline()
                    if not line:
                        break
                    if "vasp." in line.lower():
                        for tok in line.replace(":", " ").split():
                            if tok.lower().startswith("vasp."):
                                return tok.rstrip(",")
        except OSError:
            continue
    return None


def _clean(s):
    """把"其实没查到"的输出（异常栈/报错）滤掉，只留下真版本号。"""
    s = (s or "").strip()
    if not s:
        return ""
    bad = ("Traceback", "ModuleNotFoundError", "ImportError", "No module named",
           "command not found", "not found", "Permission denied", "Error")
    if any(b in s for b in bad):
        return ""
    return s.splitlines()[0][:120]


def _pkg_version(pkg):
    try:
        from importlib import metadata
        return metadata.version(pkg)
    except Exception:            # noqa: BLE001
        return None


def tool_version(name):
    """取工具版本：python 包 → 可执行 --version → vasp 从 OUTCAR 读。查不到返回 None。

    查不到就老老实实写 null（绝不把异常栈当版本号）。"""
    key = str(name).strip()
    low = key.lower()
    if low in ("vasp", "vasp_std", "vasp_gam", "vasp_ncl"):
        v = _vasp_version()
        return {"version": v, "how": "OUTCAR" if v else None}
    pkg = _PY_PKGS.get(low)
    if pkg:
        v = _pkg_version(pkg)
        if v:
            return {"version": v, "how": "pip:" + pkg}
    exe = _EXES.get(low, key)
    out = _clean(_run([exe, "--version"]))
    if out:
        return {"version": out, "how": exe + " --version"}
    if pkg:                      # 有些包只装了 import 名，没装 CLI
        out = _clean(_run([sys.executable or "python3", "-c",
                           "import importlib.metadata as m;print(m.version('%s'))" % pkg]))
        if out:
            return {"version": out, "how": "python -m importlib.metadata"}
    return {"version": None, "how": None}


def job_facts():
    """SLURM 作业事实（在计算节点上有意义；登录节点上大多是 None）。"""
    env = os.environ
    keys = {"job_id": "SLURM_JOB_ID", "job_name": "SLURM_JOB_NAME",
            "partition": "SLURM_JOB_PARTITION", "nodes": "SLURM_JOB_NODELIST",
            "cpus": "SLURM_CPUS_ON_NODE", "gres": "SLURM_JOB_GRES",
            "account": "SLURM_JOB_ACCOUNT"}
    out = {k: env.get(v) for k, v in keys.items() if env.get(v)}
    out["hostname"] = socket.gethostname()
    return out


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _find_base(step):
    """找 tf 已经写好的那份（材料目录下 provenance/<步骤>.json）并作为合并基底。"""
    for p in (os.path.join("provenance", "%s.json" % step) if step else "",
              PROV_NAME):
        if p and os.path.isfile(p):
            return p, _load(p)
    return None, {}


def record(step=None, skill=None, inputs=(), outputs=(), tools=(), extra=None,
           outdir=None, note=None, quiet=False):
    """把工具版本/产物/作业事实并入本步档案，返回最终 dict（失败返回 {}）。"""
    try:
        step = str(step or os.path.basename(os.getcwd())).strip()
        outdir = outdir or (step if os.path.isdir(step) else ".")
        base_path, prov = _find_base(step)
        prov.setdefault("schema", 1)
        prov.setdefault("kind", "gen" if not os.environ.get("SLURM_JOB_ID") else "job")
        prov.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
        if skill:
            prov.setdefault("skill", {})
            if isinstance(prov["skill"], dict):
                prov["skill"].setdefault("key", skill)
        if step:
            prov["step"] = step
        prov["updated_ts"] = datetime.datetime.now().isoformat(timespec="seconds")

        ins = prov.setdefault("inputs", {})
        for f in (inputs or []):
            # tf 基底里已经有这条时【不覆盖】它：基底记的是"从哪来、推过去的是哪份"
            # （source / origin / src_sha256 / rendered），比我们现场再哈希一遍有用；
            # 我们只把自己看到的现状挂到 post_gen 下——两者不同恰好说明
            # "gen 脚本就地改过这个文件"（如 POSCAR 被原胞化），是有价值的信息。
            name = str(f)
            mine = _file_entry(f)
            old = ins.get(name)
            if isinstance(old, dict) and old.get("sha256"):
                if old.get("sha256") != mine.get("sha256"):
                    old["post_gen"] = mine
            else:
                ins[name] = mine
        outs = prov.setdefault("outputs", {})
        for f in (outputs or []):
            outs[str(f)] = _file_entry(f)
        tls = prov.setdefault("tools", {})
        for t in (tools or []):
            tls[str(t)] = tool_version(t)
        prov.update({k: v for k, v in job_facts().items() if v})
        if note:
            prov["note"] = "%s%s" % (prov.get("note", "") and prov["note"] + " | " or "", note)
        if extra and isinstance(extra, dict):
            prov.update(extra)

        dst = os.path.join(outdir, PROV_NAME)
        tmp = dst + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(prov, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, dst)
        if not quiet:
            print("[provenance] 已写 %s（输入 %d · 产物 %d · 工具 %d）%s"
                  % (dst, len(prov.get("inputs") or {}), len(prov.get("outputs") or {}),
                     len(prov.get("tools") or {}),
                     "（基底 %s）" % base_path if base_path else ""))
        return prov
    except Exception as e:       # noqa: BLE001 —— 档案绝不能拖垮计算
        print("[provenance] 警告：记录失败（不影响计算）：%s" % e, file=sys.stderr)
        return {}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    kw = {"step": None, "skill": None, "inputs": [], "outputs": [], "tools": [],
          "note": None, "outdir": None}
    show = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--show":
            show = True
            i += 1
        elif a in ("--step", "--skill", "--note", "--outdir"):
            key = {"--step": "step", "--skill": "skill", "--note": "note",
                   "--outdir": "outdir"}[a]
            kw[key] = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
        elif a in ("--inputs", "--outputs", "--tools"):
            key = a[2:]
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                kw[key].append(argv[i])
                i += 1
        else:
            print("未知参数：%s（看文件头的用法）" % a)
            return 2
    if show:
        p = os.path.join(kw["outdir"] or ".", PROV_NAME)
        print(json.dumps(_load(p) or {"_": "没有 %s" % p}, ensure_ascii=False, indent=2))
        return 0
    prov = record(**kw)
    return 0 if prov else 1


if __name__ == "__main__":
    raise SystemExit(main())
