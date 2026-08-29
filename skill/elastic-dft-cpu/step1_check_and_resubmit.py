#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
step1_check_and_resubmit.py — 结构优化(ISIF=3)收敛检查 + 自动重投（轨迹感知）

判据不只看 VASP 的二值旗标 "reached required accuracy"（近极小值震荡时它可能永不
亮），而是读 OSZICAR 的离子步能量轨迹做诊断，按情况决定"进下一步 / 续算 / 停手"：

  力达标 + 压力达标(3D)/2D              -> converged(0)   进下一步
  stalled / 小振荡（已在极小值）        -> converged(0)   有效收敛，进下一步*
  力达标但压力超标 + 轨迹已停           -> converged(0)   Pulay 极限，接受
  progressing / nsw（还在有效下降）     -> 续算(10)       cp CONTCAR POSCAR 重投
  electronic / thrown / 大振荡（病态）  -> 停手(30)       重投同参无益，交人工
  * 需要更严几何可切 IBRION=1；默认接受"能量已落底"为有效收敛（--no-accept-stalled 关闭）。

2D 感知：2D 冻结 c 轴，真空方向残余外压属正常，故 2D 不判外压、交给力/轨迹。
维度从 workflow_method.txt 的 DIM= 继承，缺失按真空层判定。

给 agent 的约定：stdout 一行 JSON；stderr 日志；退出码
  0 converged / 10 resubmitted|not_converged(check-only) / 20 running /
  30 停手(max_restarts 或病态轨迹) / 40 error

用法：
  python step1_check_and_resubmit.py                     # 查同级 step1_std_opt
  python step1_check_and_resubmit.py <dir> --pressure-tol 5 --max-restarts 3
  python step1_check_and_resubmit.py --check-only        # 只诊断不动作
  python step1_check_and_resubmit.py --no-accept-stalled # 停滞/小振荡改为交人工
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from dim_common import read_dim, detect_dimension
    _HAS_DIM = True
except Exception:
    _HAS_DIM = False

STATE_FILE = ".relax_state.json"
METHOD_FILE = "workflow_method.txt"

# ===================================================================
#  轨迹诊断（判断离子步能量是否还在有效下降；与 tf 主程序同一套判据）
#  —— 只读 OSZICAR（每步几十字节），必要时读 INCAR 拿 NSW/NELM。
# ===================================================================
RELAX_DIAG = {
    "window":    8,       # 看最后多少个离子步
    "osc_tol":   5e-3,    # eV，|dE| 小于它且符号乱翻 = 小幅振荡
    "stall_tol": 1e-4,    # eV，|dE| 小于它但力没收敛 = 停滞
    "jump_tol":  0.5,     # eV，单步能量上涨超过它 = 线搜索把结构甩飞
    "min_steps": 6,       # 少于这么多步不下结论
}


def _tail(path, nbytes=4000000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def _incar_vals(d):
    v = {}
    try:
        with open(os.path.join(d, "INCAR"), encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.split("#")[0].split("!")[0]
                for part in ln.split(";"):
                    if "=" in part:
                        k, x = part.split("=", 1)
                        v[k.strip().upper()] = x.strip()
    except OSError:
        pass
    return v


def read_oszicar_ionic(d):
    """返回 [(离子步号, F, dE, 该步电子迭代次数), ...]。"""
    p = os.path.join(d, "OSZICAR")
    if not os.path.isfile(p):
        return []
    out, nelec = [], 0
    for ln in _tail(p).splitlines():
        m = re.match(r"\s*(DAV|RMM|CG|EDDAV|DIIS|BLK)\s*:\s*(\d+)", ln)
        if m:
            nelec = int(m.group(2))
            continue
        m = re.match(r"\s*(\d+)\s+F=\s*(\S+)\s+E0=\s*(\S+)\s+d\s*E\s*=\s*(\S+)", ln)
        if m:
            try:
                out.append((int(m.group(1)), float(m.group(2)),
                            float(m.group(4)), nelec))
            except ValueError:
                pass
            nelec = 0
    return out


def relax_diagnose(d):
    """返回 (verdict, text)。verdict ∈ progressing/oscillating/stalled/
       thrown/electronic/nsw/unknown。"""
    c = RELAX_DIAG
    steps = read_oszicar_ionic(d)
    n = len(steps)
    if n == 0:
        return "unknown", "还没有完成的离子步"
    iv = _incar_vals(d)

    def _i(k, dflt):
        try:
            return int(float(iv.get(k, dflt)))
        except (TypeError, ValueError):
            return dflt
    nsw, nelm = _i("NSW", 0), _i("NELM", 60)
    ibrion = iv.get("IBRION", "?")
    bits = ["%d 步" % n]

    W = min(n, int(c["window"]))
    win = steps[-W:]
    dEs = [x[2] for x in win]

    # 1. 电子步撞 NELM（只看窗口内近几步；首步撞 NELM 很常见、不代表整体坏）
    bad = [s for s in win if s[3] >= nelm]
    if bad:
        return "electronic", ("%s；近 %d 步里有 %d 步电子循环撞了 NELM=%d，力不可信 "
                              "-> 先调大 NELM 或放宽 EDIFF，别继续弛豫"
                              % ("/".join(bits), W, len(bad), nelm))

    # 2. 单步能量暴涨：CG 线搜索把结构甩飞
    up = max(dEs)
    if up > c["jump_tol"]:
        return "thrown", ("%s；最近有一步能量上涨 %.3f eV —— CG 线搜索把结构甩飞了 "
                          "-> 先跑 ISIF=2 固定胞弛豫原子，或 POTIM 调到 0.1"
                          % ("/".join(bits), up))

    if n < int(c["min_steps"]):
        return "progressing", "%s；步数还少，继续观察" % "/".join(bits)

    flips = sum(1 for i in range(1, len(dEs)) if dEs[i] * dEs[i - 1] < 0)
    amax = max(abs(x) for x in dEs)
    net = sum(dEs)
    bits.append("末 %d 步 dE 翻转 %d 次，|dE|max=%.2g eV，净变化 %+.2g eV"
                % (W, flips, amax, net))
    hit_nsw = (nsw and n >= nsw)
    if hit_nsw:
        bits.append("已撞 NSW=%d" % nsw)

    # 3. 振荡
    if flips >= max(2, (W - 1) // 2):
        if amax < c["osc_tol"]:
            adv = ("小幅振荡，已在极小值附近 -> 换 IBRION=1 收尾，通常 10 步内落底"
                   if str(ibrion).strip() != "1"
                   else "IBRION=1 仍小幅振荡 -> 检查 EDIFFG 是否过严 / EDIFF 是否够紧")
        else:
            adv = ("大幅振荡（|dE| 到 %.2g eV）-> 两组自由度打架，"
                   "先跑 ISIF=2 固定胞弛豫原子再放开晶胞" % amax)
        return "oscillating", "%s；%s" % ("；".join(bits), adv)

    # 4. 停滞
    if amax < c["stall_tol"]:
        return "stalled", ("%s；能量基本不动但力还没到判据 -> 换 IBRION=1 收尾；"
                           "若已是 IBRION=1，多半是 EDIFFG 过严或 EDIFF/NELM 不足"
                           % "；".join(bits))
    if hit_nsw:
        return "nsw", ("%s；还在往下走但步数用完 -> cp CONTCAR POSCAR 续跑"
                       % "；".join(bits))
    return "progressing", "%s；仍在正常下降" % "；".join(bits)


def oscillation_is_big(d):
    steps = read_oszicar_ionic(d)
    W = min(len(steps), int(RELAX_DIAG["window"]))
    if W < 2:
        return False
    return max(abs(x[2]) for x in steps[-W:]) >= float(RELAX_DIAG["osc_tol"])


# ===================================================================
#  基础工具
# ===================================================================
def log(msg):
    print(msg, file=sys.stderr, flush=True)


def emit(result, code):
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(code)


def load_state(job_dir):
    p = job_dir / STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"restart_count": 0, "last_jobid": None, "history": []}


def save_state(job_dir, state):
    (job_dir / STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_outcar(outcar):
    info = {"force_converged": False, "pressure_kb": None,
            "energy_sigma0": None, "n_ionic_steps": 0}
    if not outcar.exists():
        return info
    text = outcar.read_text(errors="ignore")
    if "reached required accuracy" in text:
        info["force_converged"] = True
    press = re.findall(r"external pressure\s*=\s*(-?[\d.]+)\s*kB", text)
    if press:
        info["pressure_kb"] = float(press[-1])
    e0 = re.findall(r"energy\(sigma->0\)\s*=\s*(-?[\d.]+)", text)
    if e0:
        info["energy_sigma0"] = float(e0[-1])
    info["n_ionic_steps"] = text.count("TOTAL-FORCE")
    return info


def job_is_active(jobid):
    if not jobid:
        return False
    try:
        out = subprocess.run(["squeue", "-h", "-j", str(jobid)],
                             capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return bool(out.stdout.strip())


def contcar_is_valid(contcar):
    if not contcar.exists() or contcar.stat().st_size == 0:
        return False
    lines = contcar.read_text(errors="ignore").splitlines()
    if len(lines) < 8:
        return False
    try:
        for i in (2, 3, 4):
            if len(lines[i].split()) < 3:
                return False
            [float(v) for v in lines[i].split()[:3]]
    except (ValueError, IndexError):
        return False
    return True


def resolve_dim(job_dir):
    if not _HAS_DIM:
        return "3d"
    dim = read_dim(job_dir / METHOD_FILE)
    if dim:
        return dim
    for name in ("CONTCAR", "POSCAR"):
        p = job_dir / name
        if p.exists():
            try:
                d, _a, _v = detect_dimension(str(p))
                return d
            except SystemExit:
                return "3d"
    return "3d"


def archive_and_clean(job_dir, run_index):
    keep = ["OUTCAR", "queue.out", "queue.err", "CONTCAR",
            "INCAR", "KPOINTS", "POTCAR", "POSCAR", "submit.sh",
            "KPOINTS_OPT", "OPTCELL", "workflow_method.txt",
            # 2D 多段弛豫的输入，必须随重投保留
            "run_relax.sh", "INCAR.s1_isif2", "INCAR.s2_isif3", "INCAR.s3_finish"]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = job_dir / f"run_{run_index:02d}_{stamp}.tar.gz"
    packed = []
    with tarfile.open(archive, "w:gz") as tar:
        for name in keep:
            src = job_dir / name
            if src.exists():
                tar.add(src, arcname=f"run_{run_index:02d}/{name}")
                packed.append(name)
    log(f"[archive] 打包 {len(packed)} 个文件 -> {archive.name}")
    protected = set(keep) | {archive.name, STATE_FILE}
    vasp_outputs = ["WAVECAR", "CHGCAR", "CHG", "vasprun.xml", "DOSCAR", "EIGENVAL",
                    "OSZICAR", "XDATCAR", "PCDAT", "IBZKPT", "WAVEDER", "REPORT",
                    "PROCAR", "LOCPOT", "ELFCAR", "PROOUT", "TMPCAR", "DYNMAT",
                    "vaspout.h5", "vaspwave.h5"]
    removed = []
    for name in vasp_outputs:
        f = job_dir / name
        if name not in protected and f.is_file():
            try:
                f.unlink(); removed.append(name)
            except OSError as exc:
                log(f"[warn] 删除 {name} 失败: {exc}")
    log(f"[clean] 删除 {len(removed)} 个中间输出")


def resubmit(job_dir, submit_cmd):
    try:
        out = subprocess.run(submit_cmd, shell=True, cwd=job_dir,
                             capture_output=True, text=True, timeout=120)
    except subprocess.SubprocessError as exc:
        return False, None, f"提交异常: {exc}"
    combined = (out.stdout + out.stderr).strip()
    if out.returncode != 0:
        return False, None, combined
    m = re.search(r"Submitted batch job\s+(\d+)", combined)
    return True, (m.group(1) if m else None), combined


def do_resubmit(job_dir, args, state, base, reason, contcar):
    if state["restart_count"] >= args.max_restarts:
        emit({**base, "status": "max_restarts_exceeded", "action": "none",
              "hint": f"{reason}；已达最大重启次数，交人工"}, 30)
    if not contcar_is_valid(contcar):
        emit({**base, "status": "error",
              "reason": f"{reason}；CONTCAR 无效/截断，无法续算"}, 40)
    if not (job_dir / args.submit_cmd.split()[-1]).exists():
        emit({**base, "status": "error",
              "reason": f"{reason}；找不到提交脚本"}, 40)
    run_index = state["restart_count"] + 1
    archive_and_clean(job_dir, run_index)
    shutil.copy2(contcar, job_dir / "POSCAR")
    log("[restart] cp CONTCAR POSCAR，用弛豫后结构续算")
    ok, new_jobid, echo = resubmit(job_dir, args.submit_cmd)
    if not ok:
        emit({**base, "status": "error", "reason": f"{reason}；重投失败: {echo}"}, 40)
    state.update({"restart_count": run_index, "last_jobid": new_jobid})
    state["history"].append({"at": base["checked_at"], "run_index": run_index,
                             "verdict": base.get("verdict"),
                             "pressure_kb": base.get("external_pressure_kb"),
                             "new_jobid": new_jobid})
    save_state(job_dir, state)
    log(f"[resubmitted] 第 {run_index} 次重投，新 jobid={new_jobid}")
    emit({**base, "status": "resubmitted", "restart_count": run_index,
          "new_jobid": new_jobid, "hint": "等作业结束后再次调用本脚本复查"}, 10)


def main():
    ap = argparse.ArgumentParser(
        description="结构优化收敛检查 + 自动重投（轨迹感知，2D 感知）")
    ap.add_argument("job_dir", nargs="?", default=None)
    ap.add_argument("--pressure-tol", type=float, default=5.0,
                    help="3D 残余外压达标阈值 |P|<=该值(kB)，默认 5.0；2D 忽略外压")
    ap.add_argument("--max-restarts", type=int, default=3)
    ap.add_argument("--submit-cmd", default="sbatch submit.sh")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--no-accept-stalled", dest="accept_stalled",
                    action="store_false",
                    help="停滞/小振荡默认视为有效收敛放行；加此项改为交人工")
    args = ap.parse_args()

    job_dir = (Path(__file__).resolve().parent / "step1_std_opt"
               if args.job_dir is None else Path(args.job_dir).expanduser()).resolve()
    now = datetime.now().isoformat(timespec="seconds")
    base = {"job_dir": str(job_dir), "checked_at": now, "pressure_tol_kb": args.pressure_tol}

    if not job_dir.is_dir():
        emit({**base, "status": "error", "reason": f"目录不存在: {job_dir}"}, 40)

    state = load_state(job_dir)
    if job_is_active(state.get("last_jobid")):
        emit({**base, "status": "running", "jobid": state["last_jobid"],
              "restart_count": state["restart_count"],
              "reason": "作业仍在排队/运行，稍后重新检查"}, 20)

    outcar, contcar = job_dir / "OUTCAR", job_dir / "CONTCAR"
    info = parse_outcar(outcar)
    dim = resolve_dim(job_dir)
    verdict, vtext = relax_diagnose(str(job_dir))
    big_osc = oscillation_is_big(str(job_dir))

    base.update({
        "dimension": dim.upper(), "verdict": verdict, "verdict_detail": vtext,
        "force_converged": info["force_converged"],
        "external_pressure_kb": info["pressure_kb"],
        "energy_sigma0_eV": info["energy_sigma0"],
        "n_ionic_steps": info["n_ionic_steps"],
        "restart_count": state["restart_count"],
    })

    if not outcar.exists() or info["n_ionic_steps"] == 0:
        emit({**base, "status": "error",
              "reason": "OUTCAR 缺失或无离子步，作业可能未启动/早崩"}, 40)

    cell_ok = True if dim == "2d" else (
        info["pressure_kb"] is not None and abs(info["pressure_kb"]) <= args.pressure_tol)
    reached = info["force_converged"]
    settled = (verdict == "stalled") or (verdict == "oscillating" and not big_osc)
    pathological = (verdict in ("electronic", "thrown")) or (verdict == "oscillating" and big_osc)

    # (1) 硬收敛
    if reached and cell_ok:
        emit({**base, "status": "converged",
              "reason": "力达 reached required accuracy"
                        + ("" if dim == "2d" else " 且残余压力达标")}, 0)
    # (2) 病态轨迹：停手（重投无益）
    if pathological:
        emit({**base, "status": "halted_pathological", "action": "none",
              "hint": vtext + "（重投同参无益，已停手）"}, 30)
    # (3) 已到极小值（停滞/小振荡）
    if settled:
        if reached:
            emit({**base, "status": "converged",
                  "reason": f"力达标且轨迹已停({verdict})——残余压力属 Pulay 极限，接受"}, 0)
        if args.accept_stalled:
            emit({**base, "status": "converged",
                  "reason": f"能量已落底({verdict})、力接近判据，视为有效收敛进下一步；"
                            f"如需更严可切 IBRION=1。{vtext}"}, 0)
        emit({**base, "status": "halted_stalled", "action": "none",
              "hint": vtext + "（--no-accept-stalled：不自动放行，建议切 IBRION=1）"}, 30)
    # (4) check-only
    if args.check_only:
        emit({**base, "status": "not_converged", "action": "none (check-only)",
              "reason": vtext}, 10)
    # (5) 还在有效下降 -> 续算
    do_resubmit(job_dir, args, state, base, vtext, contcar)


if __name__ == "__main__":
    main()
