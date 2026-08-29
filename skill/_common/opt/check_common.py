#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_common.py
===============
step2 / step3 / step4 检查+重投脚本的共享核心库。
（step1 的离子弛豫判据较特殊，保留其原有独立脚本 step1_check_and_resubmit.py。）

与 step1 脚本相同的"给 agent 的约定"：
  - stdout: 只输出一行 JSON（最终结果），供上层解析；
  - stderr: 人类可读的过程日志；
  - 退出码:
        0   converged                 该步已完成，产物齐备
        10  resubmitted / not_converged(check-only)
        20  running                   作业仍在排队/运行，稍后再查
        30  max_restarts_exceeded     重启已达上限，需要人工介入
        40  error                     缺文件 / 输出损坏 / 无法重投

静态/电子结构类步骤（NSW=0）的收敛判定：
  (a) OUTCAR 出现 "aborting loop because EDIFF is reached"  -> 电子自洽收敛
  (b) OUTCAR 出现 "General timing and accounting"           -> VASP 正常收尾
  (c) 该步的关键产物存在且大小达标（如 CHGCAR / WAVECAR / EIGENVAL）
三者同时满足才算 converged。
"""

import json
import re
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

STATE_FILE = ".check_state.json"

# 常见 VASP 大文件/中间输出（重投清理时的候选删除清单）
VASP_OUTPUTS = [
    "WAVECAR", "CHGCAR", "CHG", "vasprun.xml", "DOSCAR", "EIGENVAL",
    "OSZICAR", "XDATCAR", "PCDAT", "IBZKPT", "WAVEDER", "REPORT",
    "PROCAR", "LOCPOT", "ELFCAR", "PROOUT", "TMPCAR", "DYNMAT",
    "CONTCAR", "vaspout.h5", "vaspwave.h5",
    # KPOINTS_OPT 方案的 one-shot 产物：重投前必须清掉，否则上一轮的残留会被
    # 误认为本轮结果（*_OPT 只在自洽收敛后才写，跑挂的作业里可能是上一轮的）
    "PROCAR_OPT", "DOSCAR_OPT", "IBZKPT_OPT",
]

DEFAULT_KEEP_INPUTS = ["INCAR", "KPOINTS", "KPOINTS_OPT", "POTCAR", "POSCAR",
                       "submit.sh", "workflow_method.txt"]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def log(msg: str):
    """人类可读日志走 stderr，保持 stdout 干净（只放最终 JSON）。"""
    print(msg, file=sys.stderr, flush=True)


def emit(result: dict, exit_code: int):
    """输出机器可读 JSON 并退出。"""
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(exit_code)


def load_state(job_dir: Path) -> dict:
    path = job_dir / STATE_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"restart_count": 0, "last_jobid": None, "history": []}


def save_state(job_dir: Path, state: dict):
    (job_dir / STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def job_is_active(jobid) -> bool:
    """用 squeue 查指定 jobid 是否仍在排队/运行。查询失败时保守返回 False。"""
    if not jobid:
        return False
    try:
        out = subprocess.run(
            ["squeue", "-h", "-j", str(jobid)],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return bool(out.stdout.strip())


# ---------------------------------------------------------------------------
# 解析 OUTCAR / OSZICAR（静态 SCF）
# ---------------------------------------------------------------------------
def parse_scf(job_dir: Path) -> dict:
    """
    解析静态计算（NSW=0）的 OUTCAR/OSZICAR：
      scf_converged : 电子自洽是否达到 EDIFF
      finished      : VASP 是否正常收尾（General timing）
      energy_sigma0 : 最后一次 energy(sigma->0)
      n_elec_steps  : OSZICAR 里最后一个 SCF 块的电子步数
      nelm          : INCAR 里的 NELM（用于判断是否耗尽）
    """
    info = {
        "scf_converged": False,
        "finished": False,
        "energy_sigma0": None,
        "n_elec_steps": 0,
        "nelm": None,
    }

    outcar = job_dir / "OUTCAR"
    if outcar.exists():
        text = outcar.read_text(errors="ignore")
        if "aborting loop because EDIFF is reached" in text:
            info["scf_converged"] = True
        if "General timing and accounting" in text:
            info["finished"] = True
        e0 = re.findall(r"energy\(sigma->0\)\s*=\s*(-?[\d.]+)", text)
        if e0:
            info["energy_sigma0"] = float(e0[-1])

    oszicar = job_dir / "OSZICAR"
    if oszicar.exists():
        steps = 0
        for line in oszicar.read_text(errors="ignore").splitlines():
            s = line.strip()
            if re.match(r"^[A-Z]{2,4}:\s", s):      # DAV: / RMM: / DMP: / CG: ...
                steps += 1
            elif s.startswith("1 F="):              # 离子步收尾行，重置（静态只有 1 个块）
                pass
        info["n_elec_steps"] = steps

    incar = job_dir / "INCAR"
    if incar.exists():
        for line in incar.read_text(errors="ignore").splitlines():
            m = re.match(r"\s*NELM\s*=\s*(\d+)", line, re.IGNORECASE)
            if m:
                info["nelm"] = int(m.group(1))
    return info


def file_ok(path: Path, min_bytes: int = 1) -> bool:
    return path.exists() and path.stat().st_size >= min_bytes


# ---------------------------------------------------------------------------
# 归档 + 清理 + 重投
# ---------------------------------------------------------------------------
def archive_and_clean(job_dir: Path, run_index: int,
                      keep_outputs, preserve=(),
                      keep_inputs=DEFAULT_KEEP_INPUTS):
    """
    重投前的留档 + 清理：
      1) 把 keep_outputs + keep_inputs（存在才打包）打成 run_NN_<时间戳>.tar.gz；
      2) 删除 VASP_OUTPUTS 中、既不在保留清单也不在 preserve 里的文件。
    preserve: 绝对不能删的文件（例如 step4 热重启必需的 WAVECAR）。
    """
    keep_list = list(keep_outputs) + list(keep_inputs)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = job_dir / f"run_{run_index:02d}_{stamp}.tar.gz"
    packed = []
    with tarfile.open(archive, "w:gz") as tar:
        for name in keep_list:
            src = job_dir / name
            if src.exists():
                tar.add(src, arcname=f"run_{run_index:02d}/{name}")
                packed.append(name)
    log(f"[archive] 已打包 {len(packed)} 个文件 -> {archive.name}  ({', '.join(packed)})")

    protected = set(keep_list) | set(preserve) | {archive.name, STATE_FILE}
    removed = []
    for name in VASP_OUTPUTS:
        f = job_dir / name
        if name not in protected and f.is_file():
            try:
                f.unlink()
                removed.append(name)
            except OSError as exc:
                log(f"[warn] 删除 {name} 失败: {exc}")
    log(f"[clean] 已删除 {len(removed)} 个中间输出: {', '.join(removed) if removed else '(无)'}")


def resubmit(job_dir: Path, submit_cmd: str):
    """在 job_dir 内执行重投，返回 (成功?, jobid, 提交命令回显)。"""
    try:
        out = subprocess.run(
            submit_cmd, shell=True, cwd=job_dir,
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.SubprocessError as exc:
        return False, None, f"提交异常: {exc}"

    combined = (out.stdout + out.stderr).strip()
    if out.returncode != 0:
        return False, None, combined

    m = re.search(r"Submitted batch job\s+(\d+)", combined)
    jobid = m.group(1) if m else None
    return True, jobid, combined


# ---------------------------------------------------------------------------
# 通用驱动：静态/电子结构类步骤的 检查 -> (重投)
# ---------------------------------------------------------------------------
def run_static_check(args, step_name: str, default_dir: str,
                     deliverables, archive_keep_outputs,
                     preserve=(), pre_resubmit=None, restart_hint="",
                     extra_check=None):
    """
    args               : argparse 结果（需含 job_dir / max_restarts / submit_cmd /
                         check_only）
    deliverables       : [(文件名, 最小字节数), ...] —— 该步必须产出的文件；
                         也可以传一个 fn(job_dir) -> [(名, 字节)] 的回调，
                         用于"产物取决于输入"的情形（例如有没有 KPOINTS_OPT
                         决定能带本征值是在 EIGENVAL 里还是在 vasprun.xml 里）
    archive_keep_outputs: 重投时要打包留档的输出文件名列表
    preserve           : 清理时绝不删除的文件（热重启依赖）
    extra_check        : 可选回调 fn(job_dir) -> (ok, reason)。文件"存在且够大"之外
                         的内容级校验（例如 vasprun.xml 里到底有没有 kpoints_opt
                         的能带本征值）。返回 False 则视为未完成。
    pre_resubmit       : 可选回调 fn(job_dir) -> (ok, msg)，重投前的额外准备/校验
    restart_hint       : 达到重启上限时给人/agent 的建议
    """
    if args.job_dir is None:
        job_dir = Path(sys.argv[0]).resolve().parent / default_dir
    else:
        job_dir = Path(args.job_dir).expanduser()
    job_dir = job_dir.resolve()
    now = datetime.now().isoformat(timespec="seconds")

    base = {"step": step_name, "job_dir": str(job_dir), "checked_at": now}

    if not job_dir.is_dir():
        emit({**base, "status": "error", "reason": f"目录不存在: {job_dir}"}, 40)

    state = load_state(job_dir)

    # 1) 作业是否仍在运行 —— 在跑就别动，让 agent 稍后再查
    if job_is_active(state.get("last_jobid")):
        emit({**base, "status": "running",
              "jobid": state["last_jobid"],
              "restart_count": state["restart_count"],
              "reason": "作业仍在排队/运行，稍后重新检查"}, 20)

    # 2) 解析 SCF 结果 + 产物检查
    info = parse_scf(job_dir)
    dlv = deliverables(job_dir) if callable(deliverables) else deliverables
    missing = [name for name, nbytes in dlv
               if not file_ok(job_dir / name, nbytes)]
    base.update({
        "scf_converged": info["scf_converged"],
        "finished": info["finished"],
        "energy_sigma0_eV": info["energy_sigma0"],
        "n_elec_steps": info["n_elec_steps"],
        "nelm": info["nelm"],
        "deliverables": [n for n, _ in dlv],
        "deliverables_missing": missing,
        "restart_count": state["restart_count"],
    })

    outcar = job_dir / "OUTCAR"
    if not outcar.exists():
        emit({**base, "status": "error",
              "reason": "OUTCAR 缺失，作业可能未启动/未提交"}, 40)

    # 3) 内容级校验（文件在、够大，不代表里面有该有的东西）
    extra_ok, extra_reason = True, ""
    if extra_check is not None:
        extra_ok, extra_reason = extra_check(job_dir)
        base["extra_check"] = {"ok": extra_ok, "detail": extra_reason}

    # 4) 收敛判定：SCF 收敛 且 正常收尾 且 产物齐备 且 内容校验通过
    if info["scf_converged"] and info["finished"] and not missing and extra_ok:
        emit({**base, "status": "converged",
              "reason": "电子自洽收敛、正常收尾且产物齐备"}, 0)

    # --- 未完成：给出具体原因 ---
    reasons = []
    if not extra_ok and extra_reason:
        reasons.append(extra_reason)
    if not info["scf_converged"]:
        if info["finished"] and info["nelm"] and info["n_elec_steps"] >= info["nelm"]:
            reasons.append(f"NELM={info['nelm']} 耗尽仍未达 EDIFF（电子自洽不收敛）")
        else:
            reasons.append("电子自洽未达 EDIFF")
    if not info["finished"]:
        reasons.append("VASP 未正常收尾（可能崩溃/超时/被杀）")
    if missing:
        reasons.append("缺产物: " + ", ".join(missing))
    reason_str = "；".join(reasons)
    base["reason"] = reason_str
    log(f"[not-converged] {reason_str}")

    # 4) check-only：只报告不重投
    if args.check_only:
        emit({**base, "status": "not_converged", "action": "none (check-only)"}, 10)

    # 5) 重启次数保护
    if state["restart_count"] >= args.max_restarts:
        emit({**base, "status": "max_restarts_exceeded", "action": "none",
              "hint": restart_hint or "已达最大重启次数，建议人工检查"}, 30)

    # 6) 重投前的额外准备/校验（各步骤自定义）
    if pre_resubmit is not None:
        ok, msg = pre_resubmit(job_dir)
        if not ok:
            emit({**base, "status": "error",
                  "reason": f"{reason_str}；{msg}"}, 40)
        if msg:
            log(f"[prep] {msg}")

    submit_script = job_dir / args.submit_cmd.split()[-1]
    if not submit_script.exists():
        emit({**base, "status": "error",
              "reason": f"{reason_str}；找不到提交脚本 {submit_script.name}"}, 40)

    # 7) 打包留档 + 清理 -> 重投
    run_index = state["restart_count"] + 1
    archive_and_clean(job_dir, run_index, archive_keep_outputs, preserve=preserve)

    ok, new_jobid, echo = resubmit(job_dir, args.submit_cmd)
    if not ok:
        emit({**base, "status": "error",
              "reason": f"{reason_str}；重投失败: {echo}"}, 40)

    # 8) 更新状态
    state["restart_count"] = run_index
    state["last_jobid"] = new_jobid
    state["history"].append({
        "at": now,
        "run_index": run_index,
        "prev_energy_sigma0_eV": info["energy_sigma0"],
        "scf_converged": info["scf_converged"],
        "finished": info["finished"],
        "new_jobid": new_jobid,
    })
    save_state(job_dir, state)

    log(f"[resubmitted] 第 {run_index} 次重投，新 jobid={new_jobid}")
    emit({**base,
          "status": "resubmitted",
          "restart_count": run_index,
          "new_jobid": new_jobid,
          "submit_echo": echo,
          "hint": "agent 应等作业结束后再次调用本脚本复查"},
         10)


def make_argparser(description: str, default_dir: str):
    import argparse
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("job_dir", nargs="?", default=None,
                    help=f"作业目录，省略时默认取“脚本所在目录/{default_dir}”")
    ap.add_argument("--max-restarts", type=int, default=3,
                    help="最大自动重启次数，超过则交人工，默认 3")
    ap.add_argument("--submit-cmd", default="sbatch submit.sh",
                    help="重投命令，默认 'sbatch submit.sh'")
    ap.add_argument("--check-only", action="store_true",
                    help="只检查、绝不重投（dry-run）")
    return ap
