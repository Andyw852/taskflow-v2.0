#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
step1_check_and_resubmit.py
===========================
VASP 结构优化 (ISIF=3) 收敛检查 + 自动重投脚本。

设计目的
--------
给多智能体编排调用的“后处理/守护”步骤。它与输入生成脚本
(gen_step1_PBE_opt.py) 属于不同阶段：
    生成脚本 -> 投递作业 -> [本脚本] 检查收敛，未达标则重投

判定逻辑
--------
一次调用做三件事：
    1. 解析 job_dir 下的 OUTCAR / OSZICAR / 队列状态；
    2. 判断是否“真正收敛”：
         (a) 离子弛豫达到力判据      -> OUTCAR 出现 "reached required accuracy"
         (b) 残余外压达标            -> |external pressure| <= --pressure-tol (kB)
       两者同时满足才算 converged；
    3. 未达标且作业已结束 -> 按“备份 -> cp CONTCAR POSCAR -> 重投”重启，
       并用状态文件记录重启次数，超过 --max-restarts 就停手交给人/上层 agent。

给 agent 的约定
--------------
  - stdout: 只输出一行 JSON（最终结果），供上层解析；
  - stderr: 人类可读的过程日志；
  - 退出码:
        0   converged                 已收敛，本结构可用于能量比较
        10  resubmitted               已重投，agent 应稍后重新调用本脚本
        20  running                   作业仍在排队/运行，agent 应等待后再查
        30  max_restarts_exceeded     重启已达上限，需要人工介入
        40  error                     缺文件 / 输出损坏 / 无法重投

用法
----
    # 最简：在 band-dft-cpu/ 目录下直接运行，自动检查同级的 step1_PBE_opt
    python step1_check_and_resubmit.py

    # 显式指定作业目录
    python step1_check_and_resubmit.py /path/to/step1_PBE_opt \
        --pressure-tol 5.0 --max-restarts 3 --submit-cmd "sbatch submit.sh"

    # 只检查、绝不重投（dry-run，agent 想先看状态时用）：
    python step1_check_and_resubmit.py --check-only
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

STATE_FILE = ".relax_state.json"          # 记录重启次数与历史
# 重投前把「OUTCAR/queue.out/queue.err/CONTCAR + 输入文件」打包成
# step1 目录内的 run_NN_<时间戳>.tar.gz，其余中间输出删除。


# ---------------------------------------------------------------------------
# 小工具
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


# ---------------------------------------------------------------------------
# 解析 OUTCAR：收敛 / 压力 / 能量
# ---------------------------------------------------------------------------
def parse_outcar(outcar: Path) -> dict:
    """
    从 OUTCAR 提取判断收敛所需的信息。
    只取“最后一次”出现的值，对应最终离子步。
    """
    info = {
        "force_converged": False,   # 是否出现 reached required accuracy
        "pressure_kb": None,        # 最后一次 external pressure (kB)
        "pullay_kb": None,          # 最后一次 Pullay stress (kB)
        "energy_sigma0": None,      # 最后一次 energy(sigma->0) (eV)，用于能量比较
        "n_ionic_steps": 0,         # 已完成的离子步数
    }

    if not outcar.exists():
        return info

    text = outcar.read_text(errors="ignore")

    if "reached required accuracy" in text:
        info["force_converged"] = True

    # external pressure = X kB  Pullay stress = Y kB  （取最后一次）
    press = re.findall(
        r"external pressure\s*=\s*(-?[\d.]+)\s*kB\s*Pullay stress\s*=\s*(-?[\d.]+)\s*kB",
        text,
    )
    if press:
        info["pressure_kb"] = float(press[-1][0])
        info["pullay_kb"] = float(press[-1][1])

    # energy(sigma->0) = X （取最后一次，最终能量）
    e0 = re.findall(r"energy\(sigma->0\)\s*=\s*(-?[\d.]+)", text)
    if e0:
        info["energy_sigma0"] = float(e0[-1])

    # 已完成离子步数（每个离子步一行力汇总）
    info["n_ionic_steps"] = text.count("TOTAL-FORCE")

    return info


# ---------------------------------------------------------------------------
# 队列状态：作业是否还在跑
# ---------------------------------------------------------------------------
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
# 重投前：校验 CONTCAR 可用，并备份上一轮输出
# ---------------------------------------------------------------------------
def contcar_is_valid(contcar: Path) -> bool:
    """CONTCAR 至少要有完整头部+坐标；崩溃的作业常留下空/截断的 CONTCAR。"""
    if not contcar.exists() or contcar.stat().st_size == 0:
        return False
    lines = contcar.read_text(errors="ignore").splitlines()
    if len(lines) < 8:
        return False
    # 第 3~5 行应为晶格矢量，能解析成 3 个浮点数
    try:
        for i in (2, 3, 4):
            vals = lines[i].split()
            if len(vals) < 3:
                return False
            [float(v) for v in vals[:3]]
    except (ValueError, IndexError):
        return False
    return True


def archive_and_clean(job_dir: Path, run_index: int):
    """
    重投前的留档 + 清理：
      1) 把「保留清单」(OUTCAR/queue.out/queue.err/CONTCAR + 输入文件)
         打包成 step1 目录内的 run_NN_<时间戳>.tar.gz；
      2) 删除除保留清单之外的所有 VASP 输出（WAVECAR/CHGCAR/... 等大文件）。
    注意：CONTCAR 本身先打进包里，之后主流程再 cp CONTCAR POSCAR，顺序不冲突。
    """
    # —— 要留档的文件（存在才打包）——
    keep_outputs = ["OUTCAR", "queue.out", "queue.err", "CONTCAR"]
    # OPTCELL: 2D 约束变胞的输入（optcell_file 流派），必须随重投保留，否则重投的
    #          ISIF=3 会失去 c 轴约束、真空塌缩。3D 无此文件，列入无副作用。
    keep_inputs = ["INCAR", "KPOINTS", "POTCAR", "POSCAR", "submit.sh",
                   "KPOINTS_OPT", "OPTCELL", "workflow_method.txt"]
    keep_list = keep_outputs + keep_inputs

    # —— 打包 ——
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = job_dir / f"run_{run_index:02d}_{stamp}.tar.gz"
    packed = []
    with tarfile.open(archive, "w:gz") as tar:
        for name in keep_list:
            src = job_dir / name
            if src.exists():
                # arcname 加子目录前缀，解压后不会散落
                tar.add(src, arcname=f"run_{run_index:02d}/{name}")
                packed.append(name)
    log(f"[archive] 已打包 {len(packed)} 个文件 -> {archive.name}  ({', '.join(packed)})")

    # —— 清理：删除保留清单 + 归档包本身之外的所有 VASP 输出 ——
    # 说明：只删“文件”，不碰目录（run_history 之类）；.tar.gz 归档包永远保留。
    # OPTCELL 已在 keep_list 内（且不属于 vasp_outputs），双重保证：2D 约束文件绝不被删。
    protected = set(keep_list) | {archive.name, STATE_FILE}
    # 常见 VASP 输出（显式列出，避免误删用户自备脚本/模板）
    vasp_outputs = [
        "WAVECAR", "CHGCAR", "CHG", "vasprun.xml", "DOSCAR", "EIGENVAL",
        "OSZICAR", "XDATCAR", "PCDAT", "IBZKPT", "WAVEDER", "REPORT",
        "PROCAR", "LOCPOT", "ELFCAR", "PROOUT", "TMPCAR", "DYNMAT",
        "vaspout.h5", "vaspwave.h5",
    ]
    removed = []
    for name in vasp_outputs:
        f = job_dir / name
        if name not in protected and f.is_file():
            try:
                f.unlink()
                removed.append(name)
            except OSError as exc:
                log(f"[warn] 删除 {name} 失败: {exc}")
    log(f"[clean] 已删除 {len(removed)} 个中间输出: {', '.join(removed) if removed else '(无)'}")


# ---------------------------------------------------------------------------
# 执行重投
# ---------------------------------------------------------------------------
def resubmit(job_dir: Path, submit_cmd: str):
    """
    在 job_dir 内执行重投，返回 (成功?, jobid, 提交命令回显)。
    从 sbatch 输出里解析 "Submitted batch job <id>"。
    """
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
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="VASP 结构优化收敛检查 + 自动重投（供多智能体调用）"
    )
    ap.add_argument("job_dir", nargs="?", default=None,
                    help="作业目录（含 OUTCAR / CONTCAR / submit.sh）。"
                         "省略时默认取“脚本所在目录/step1_PBE_opt”。")
    ap.add_argument("--pressure-tol", type=float, default=5.0,
                    help="残余外压达标阈值 |external pressure| <= 该值(kB)，默认 5.0")
    ap.add_argument("--max-restarts", type=int, default=3,
                    help="最大自动重启次数，超过则交人工，默认 3")
    ap.add_argument("--submit-cmd", default="sbatch submit.sh",
                    help="重投命令，默认 'sbatch submit.sh'")
    ap.add_argument("--check-only", action="store_true",
                    help="只检查、绝不重投（dry-run）")
    args = ap.parse_args()

    # job_dir 缺省时，默认取“脚本所在目录 / step1_PBE_opt”，
    # 这样在 band-dft-cpu/ 目录下直接 `python step1_check_and_resubmit.py` 即可。
    if args.job_dir is None:
        job_dir = (Path(__file__).resolve().parent / "step1_PBE_opt")
    else:
        job_dir = Path(args.job_dir).expanduser()
    job_dir = job_dir.resolve()
    now = datetime.now().isoformat(timespec="seconds")

    base = {
        "job_dir": str(job_dir),
        "checked_at": now,
        "pressure_tol_kb": args.pressure_tol,
    }

    if not job_dir.is_dir():
        emit({**base, "status": "error",
              "reason": f"目录不存在: {job_dir}"}, 40)

    state = load_state(job_dir)

    # 1) 作业是否仍在运行 —— 在跑就别动，让 agent 稍后再查
    if job_is_active(state.get("last_jobid")):
        emit({**base, "status": "running",
              "jobid": state["last_jobid"],
              "restart_count": state["restart_count"],
              "reason": "作业仍在排队/运行，稍后重新检查"}, 20)

    # 2) 解析 OUTCAR
    outcar = job_dir / "OUTCAR"
    info = parse_outcar(outcar)
    base.update({
        "force_converged": info["force_converged"],
        "external_pressure_kb": info["pressure_kb"],
        "pullay_stress_kb": info["pullay_kb"],
        "energy_sigma0_eV": info["energy_sigma0"],
        "n_ionic_steps": info["n_ionic_steps"],
        "restart_count": state["restart_count"],
    })

    if not outcar.exists() or info["n_ionic_steps"] == 0:
        emit({**base, "status": "error",
              "reason": "OUTCAR 缺失或无离子步，作业可能未启动/早崩"}, 40)

    # 3) 收敛判定：力达标 且 压力达标
    pressure_ok = (
        info["pressure_kb"] is not None
        and abs(info["pressure_kb"]) <= args.pressure_tol
    )
    converged = info["force_converged"] and pressure_ok

    if converged:
        emit({**base, "status": "converged",
              "reason": "力已达 reached required accuracy 且残余压力达标，可用于能量比较"},
             0)

    # --- 未收敛：给出具体原因 ---
    reasons = []
    if not info["force_converged"]:
        reasons.append("未出现 reached required accuracy（离子力未达标）")
    if not pressure_ok:
        p = info["pressure_kb"]
        reasons.append(
            f"残余外压 {p} kB 超阈值 ±{args.pressure_tol} kB"
            if p is not None else "未解析到 external pressure"
        )
    reason_str = "；".join(reasons)
    base["reason"] = reason_str
    log(f"[not-converged] {reason_str}")

    # 4) check-only：只报告不重投
    if args.check_only:
        emit({**base, "status": "not_converged", "action": "none (check-only)"}, 10)

    # 5) 重启次数保护
    if state["restart_count"] >= args.max_restarts:
        emit({**base, "status": "max_restarts_exceeded",
              "action": "none",
              "hint": "已达最大重启次数，建议人工检查：POTIM 调小 / 改 IBRION=1 / 核对结构合理性"},
             30)

    # 6) 校验 CONTCAR 可用于续算
    contcar = job_dir / "CONTCAR"
    if not contcar_is_valid(contcar):
        emit({**base, "status": "error",
              "reason": f"{reason_str}；且 CONTCAR 无效/截断，无法安全续算"}, 40)

    submit_script = job_dir / args.submit_cmd.split()[-1]
    if not submit_script.exists():
        emit({**base, "status": "error",
              "reason": f"{reason_str}；找不到提交脚本 {submit_script.name}"}, 40)

    # 7) 打包留档 + 清理 -> cp CONTCAR POSCAR -> 重投
    run_index = state["restart_count"] + 1
    archive_and_clean(job_dir, run_index)

    shutil.copy2(contcar, job_dir / "POSCAR")
    log("[restart] 已 cp CONTCAR POSCAR，用弛豫后的结构续算（基组按新体积重建）")

    if args.check_only:  # 双保险
        emit({**base, "status": "not_converged", "action": "none (check-only)"}, 10)

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
        "prev_pressure_kb": info["pressure_kb"],
        "prev_energy_sigma0_eV": info["energy_sigma0"],
        "force_converged": info["force_converged"],
        "new_jobid": new_jobid,
    })
    save_state(job_dir, state)

    log(f"[resubmitted] 第 {run_index} 次重投，新 jobid={new_jobid}")
    emit({**base,
          "status": "resubmitted",
          "action": "cp CONTCAR POSCAR + resubmit",
          "restart_count": run_index,
          "new_jobid": new_jobid,
          "submit_echo": echo,
          "hint": "agent 应等作业结束后再次调用本脚本复查"},
         10)


if __name__ == "__main__":
    main()