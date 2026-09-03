#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_jobs.py —— 探测 VASP 作业计算健康度（只读，绝不提交/不改文件）。

判断每个作业是：正常弛豫(relaxing) / 已收敛(done) / SCF 发散(scf_stuck) /
步数用尽(nsw) / 崩溃(crashed) / 掉队(dead) / 排队(queued)。

输出：结构化 JSON（每作业的判据 + 结论），打印到 stdout，并写到
probe_out/ 临时目录（供 AI agent 读取判断，省 token）。

用法：
  python3 probe_jobs.py -p Sn2Sb2Te5              # 探测该材料所有未完成作业
  python3 probe_jobs.py -p Sn2Sb2Te5 -j S2_def    # 只探测某步骤（label）
  python3 probe_jobs.py -p Sn2Sb2Te5,Pb2Sb2Te5    # 多材料逗号分隔
  python3 probe_jobs.py -p Sn2Sb2Te5 --host jzzn  # 指定超算（默认 jzzn）

不依赖 tf，可独立运行；也可被 agent 直接调用。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "probe_out")

# 远端采集脚本：在超算上读 OSZICAR/OUTCAR/queue.err/squeue，输出紧凑 JSON。
# argv[1]=材料目录绝对路径, argv[2]=步骤名, argv[3]=作业目录名列表(逗号分隔)。
REMOTE_GATHER = r'''
import json, os, glob, sys, subprocess

matdir, step, fanout = sys.argv[1], sys.argv[2], sys.argv[3]
stepdir = os.path.join(matdir, step)

def ionic_steps(osz):
    out = []
    try:
        for line in open(osz, errors="ignore"):
            t = line.split()
            if len(t) >= 4 and t[0].isdigit() and t[1] == "F=":
                out.append([int(t[0]), float(t[2])])
    except OSError:
        pass
    return out

def err_sig(d):
    p = os.path.join(d, "queue.err")
    if not os.path.isfile(p):
        return ""
    try:
        txt = open(p, errors="ignore").read()
    except OSError:
        return ""
    for key in ("RHOPS /= RHOAE", "MPI_Abort", "VERY BAD NEWS", "POSMAP",
                "NODE_FAIL", "DUE TO TIME LIMIT", "CANCELLED", "segmentation",
                "Stack trace terminated", "SIGSEGV", "OOM", "out of memory"):
        if key in txt:
            return key
    return ""

def read_nsw(d):
    p = os.path.join(d, "INCAR")
    try:
        for line in open(p, errors="ignore"):
            if line.strip().startswith("NSW"):
                try:
                    return int(line.split("=")[1].split()[0])
                except (ValueError, IndexError):
                    pass
    except OSError:
        pass
    return 100

# squeue 全名（不截断）
sq = {}
try:
    for line in subprocess.run(["squeue", "-u", os.environ.get("USER", "wangchao"),
                                "-h", "-o", "%i %j %T"],
                               capture_output=True, text=True).stdout.splitlines():
        t = line.split()
        if len(t) >= 3:
            sq[t[1]] = (t[0], t[2])
except Exception:
    pass

def scan_jobs(dirs):
    jobs = []
    for d in dirs:
        name = os.path.basename(d.rstrip("/"))
        o = os.path.join(d, "OUTCAR")
        done = os.path.isfile(o) and ("reached required accuracy" in open(o, errors="ignore").read())
        steps = ionic_steps(os.path.join(d, "OSZICAR"))
        q = sq.get(name) or sq.get("ref_" + name)
        if q is None:
            for jn, v in sq.items():
                if jn.endswith("-" + name):
                    q = v
                    break
        jobs.append({
            "name": name,
            "done": done,
            "steps": steps,
            "nsw": read_nsw(d),
            "err": err_sig(d),
            "in_queue": q is not None,
            "qstate": (q[1] if q else None),
            "qid": (q[0] if q else None),
        })
    return jobs

dirs = sorted(glob.glob(os.path.join(stepdir, "def-*"))) if fanout else [stepdir]
# 参考相单独处理（S0 的共享目录）
if step == "step0_references" and not dirs:
    ref = os.path.join(matdir, "..", "..", "convex_hull_refs", "convex_hull_references")
    dirs = sorted(glob.glob(os.path.join(ref, "*"))) if os.path.isdir(ref) else []
res = {"matdir": matdir, "step": step, "jobs": scan_jobs(dirs)}
print(json.dumps(res))
'''


def classify(job):
    """把一个作业的原始数据判成状态 + 中文结论。"""
    name = job["name"]
    if job["done"]:
        return {"status": "done", "status_cn": "已收敛",
                "evidence": "OUTCAR 含 reached required accuracy",
                "conclusion": "已完成，无需处理"}
    if job["err"]:
        e = job["err"]
        if e in ("RHOPS /= RHOAE", "MPI_Abort", "VERY BAD NEWS", "POSMAP", "SIGSEGV"):
            return {"status": "crashed", "status_cn": "VASP 崩溃",
                    "evidence": "queue.err 含 %s" % e,
                    "conclusion": "计算崩溃（%s），需诊断结构/参数后重交" % e}
        if e == "DUE TO TIME LIMIT":
            return {"status": "nsw", "status_cn": "超时",
                    "evidence": "queue.err 含 DUE TO TIME LIMIT，已跑 %d 步" % len(job["steps"]),
                    "conclusion": "超时被砍，cp CONTCAR POSCAR 续跑"}
        return {"status": "crashed", "status_cn": "节点/系统故障",
                "evidence": "queue.err 含 %s" % e,
                "conclusion": "节点/系统故障（%s），重交即可" % e}
    steps = job["steps"]
    if not steps:
        if job["in_queue"]:
            return {"status": "queued", "status_cn": "排队中",
                    "evidence": "队列状态 %s，尚无 OSZICAR" % job["qstate"],
                    "conclusion": "等节点，无需处理"}
        return {"status": "dead", "status_cn": "掉队",
                "evidence": "不在队列且无进度",
                "conclusion": "不在队列且无进度，需重交"}
    n = len(steps)
    e_first, e_last = steps[0][1], steps[-1][1]
    de = e_last - e_first
    # SCF 发散：相邻离子步能量大幅振荡（来回跳 > 5 eV 且符号交替）
    osc = 0
    for i in range(1, len(steps)):
        d = steps[i][1] - steps[i - 1][1]
        if abs(d) > 5.0 and (i >= 2 and (steps[i][1] - steps[i - 1][1]) * (steps[i - 1][1] - steps[i - 2][1]) < 0):
            osc += 1
    if osc >= 3:
        return {"status": "scf_stuck", "status_cn": "SCF 发散/电荷晃动",
                "evidence": "能量大幅振荡 %d 次（初 %.2f → 末 %.2f eV）" % (osc, e_first, e_last),
                "conclusion": "SCF 不收敛（电荷晃动），建议 ALGO=All + AMIX=0.1 + BMIX=0.0001"}
    if n >= job["nsw"] and not job["in_queue"] and de < 0:
        return {"status": "nsw", "status_cn": "步数用尽",
                "evidence": "跑到 NSW=%d 步未收敛，能量仍下降（ΔE=%.2f eV）" % (job["nsw"], de),
                "conclusion": "步数用尽但仍在下行，cp CONTCAR POSCAR 续跑"}
    # 正常下降
    tail = "、".join("%.2f" % steps[i][1] for i in range(max(0, n - 3), n))
    return {"status": "relaxing", "status_cn": "正常弛豫中",
            "evidence": "%d 步，能量 %.2f→%.2f eV（ΔE=%.2f，单调下降，末三步 %s）" % (
                n, e_first, e_last, de, tail),
            "conclusion": "正常弛豫，预计还需若干步收敛，无需处理"}


def main():
    ap = argparse.ArgumentParser(description="探测 VASP 作业健康度（只读）")
    ap.add_argument("-p", "--projects", required=True, help="材料名，逗号分隔")
    ap.add_argument("-j", "--job", default="step2_defects", help="步骤名/label，默认 step2_defects")
    ap.add_argument("--host", default="jzzn", help="超算 ssh 别名，默认 jzzn")
    ap.add_argument("--work-dir", default="/public/home/wangchao/defect_work",
                    help="超算工作根目录")
    ap.add_argument("--raw", action="store_true", help="只打印远端原始数据")
    args = ap.parse_args()

    # 步骤 label -> 目录名
    label2dir = {"S0_refs": "step0_references", "S0": "step0_references",
                 "S1_bulk": "step1_bulk", "S1": "step1_bulk",
                 "S2_def": "step2_defects", "S2": "step2_defects",
                 "S3_chg": "step3_charged", "S3": "step3_charged",
                 "S4_anlys": "step4_analysis", "S4": "step4_analysis"}
    step = args.job if "/" in args.job or args.job.startswith("step") else label2dir.get(args.job, args.job)
    fanout = step in ("step2_defects", "step3_charged")

    all_jobs = []
    for mat in [m.strip() for m in args.projects.split(",") if m.strip()]:
        matdir = "%s/%s/defect-dft-cpu" % (args.work_dir.rstrip("/"), mat)
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", args.host, "python3", "-", matdir, step, "1" if fanout else "0"],
            input=REMOTE_GATHER, capture_output=True, text=True)
        if proc.returncode != 0:
            print(json.dumps({"error": "ssh 失败", "detail": proc.stderr[-500:]},
                             ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            print(json.dumps({"error": "远端输出解析失败", "raw": proc.stdout[-500:]},
                             ensure_ascii=False), file=sys.stderr)
            sys.exit(1)
        if args.raw:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            continue
        jobs = [{"material": mat, "name": j["name"], **classify(j)} for j in data["jobs"]]
        all_jobs.extend(jobs)

    if args.raw:
        return
    # 汇总
    from collections import Counter
    c = Counter(j["status"] for j in all_jobs)
    summary = "共 %d 作业：%s" % (len(all_jobs),
                                " ".join("%s=%d" % (k, v) for k, v in sorted(c.items())))
    result = {"probe_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "materials": [m.strip() for m in args.projects.split(",") if m.strip()],
              "step": step, "summary": summary, "jobs": all_jobs}
    os.makedirs(OUTDIR, exist_ok=True)
    outfile = os.path.join(OUTDIR, "probe_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("[临时文件] %s" % outfile, file=sys.stderr)


if __name__ == "__main__":
    main()
