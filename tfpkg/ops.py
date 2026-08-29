# -*- coding: utf-8 -*-
"""ops —— 运维流（12_hang+14_init+15_hpc+16_watch 合并）。
挂死检测/恢复 + 项目初始化 + hpc 切换 + 后台监控。
对外接口：auto_recover_hung / cmd_init / cmd_hpc / cmd_watch 等。"""

import os
import sys
import re
import json
import time
import shlex
import hashlib
import base64
import collections
import functools
import itertools
import subprocess
import tempfile
import threading
import socket
import argparse
import glob
import math
import random
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

# ===== 来自 12_hang.py =====
# -*- coding: utf-8 -*-
# 12_hang —— 挂死作业检测与自动恢复
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L4373  _hung_cfg
#   L4384  _hung_enabled
#   L4388  _hung_state_path
#   L4394  _hung_state_load
#   L4403  _hung_state_save
#   L4412  _hung_scan
#   L4502  _material_of_workdir
#   L4512  _hung_scf_rms_trend
#   L4530  _hung_incar_fix
#   L4580  _hung_scancel_wait
#   L4596  _hung_resume
#   L4615  auto_recover_hung

# ===== _hung_cfg (原 L4373-L4381) =====
def _hung_cfg(cfg, t, st, key, dflt):
    """挂死恢复参数取值优先级：项目 setting.yaml > 技能 task_types > 全局 tf.yaml > 默认。"""
    if st and key in st:
        return st[key]
    if t and t.get(key) is not None:
        return t[key]
    if cfg and cfg.get(key) is not None:
        return cfg[key]
    return dflt

# ===== _hung_enabled (原 L4384-L4385) =====
def _hung_enabled(cfg, t=None, st=None):
    return bool(_hung_cfg(cfg, t, st, "hang_check", True))

# ===== _hung_state_path (原 L4388-L4391) =====
def _hung_state_path(cfg):
    """挂死重试计数文件：<配置目录>/.tf_hung.json（workdir → 已恢复次数）。"""
    d = cfg.get("_config_dir") or "."
    return os.path.join(d, ".tf_hung.json")

# ===== _hung_state_load (原 L4394-L4400) =====
def _hung_state_load(cfg):
    try:
        with open(_hung_state_path(cfg), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}

# ===== _hung_state_save (原 L4403-L4409) =====
def _hung_state_save(cfg, state):
    try:
        os.makedirs(os.path.dirname(_hung_state_path(cfg)), exist_ok=True)
        with open(_hung_state_path(cfg), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except OSError:
        pass

# ===== _hung_scan (原 L4412-L4499) =====
def _hung_scan(cfg, host="__default__"):
    from tfpkg import run_remote
    '''远端一次性扫描：挂死检测 + 原因诊断合并为一次调用。
    返回 (rc, text)，text 为 JSON 数组，每个作业一条：
      {jobid, wd, running, age, bytes, lines, last3, err_node, err_disk, algo, amix, nelm}
    - running: True=squeue RUNNING；False=sacct 查到的 NODE_FAIL（节点挂了会离开 RUNNING）
    - age: OUTCAR/OSZICAR 里较新的那个距现在秒数
    - bytes/lines: OUTCAR 字节数 / OSZICAR 行数（进度指纹：判断活着还是卡死）
    - last3: OSZICAR 最后 3 行（判断是否卡在 SCF 迭代、rms 是否还在降）
    - err_node/err_disk: queue.err 是否有 NODE FAILURE / No space
    - algo/amix/nelm: INCAR 当前状态（供 SCF 升级决策）'''
    code = r'''
import subprocess, os, time, json, getpass, re
u = getpass.getuser()
now = time.time()
out = []
try:
    r = subprocess.run(["squeue", "-u", u, "-h", "-o", "%i|%Z|%T"],
                       capture_output=True, text=True, timeout=60)
    for ln in r.stdout.splitlines():
        p = ln.split("|")
        if len(p) < 3 or p[2] != "RUNNING":
            continue
        jid, wd = p[0], p[1]
        if not wd:
            continue
        rec = {"jobid": jid, "wd": wd, "running": True, "age": None,
               "bytes": 0, "lines": 0, "last3": [], "err_node": False,
               "err_disk": False, "algo": "", "amix": False, "nelm": 0}
        for f in ("OUTCAR", "OSZICAR"):
            path = os.path.join(wd, f)
            if not os.path.isfile(path):
                continue
            st = os.stat(path)
            a = now - st.st_mtime
            if rec["age"] is None or a < rec["age"]:
                rec["age"] = round(a, 1)
            if f == "OUTCAR":
                rec["bytes"] = st.st_size
            else:
                try:
                    rec["lines"] = sum(1 for _ in open(path, errors="ignore"))
                except Exception:
                    rec["lines"] = 0
                try:
                    lines = open(path, errors="ignore").read().strip().splitlines()
                    rec["last3"] = lines[-3:]
                except Exception:
                    pass
        try:
            q = open(os.path.join(wd, "queue.err"), errors="ignore").read()[-3000:].upper()
            rec["err_node"] = "NODE FAILURE" in q
            rec["err_disk"] = ("NO SPACE" in q) or ("DISK QUOTA" in q)
        except Exception:
            pass
        try:
            inc = open(os.path.join(wd, "INCAR"), errors="ignore").read()
            m = re.search(r"^\s*ALGO\s*=\s*(\S+)", inc, re.M)
            rec["algo"] = m.group(1) if m else ""
            rec["amix"] = bool(re.search(r"^\s*AMIX\s*=", inc, re.M))
            m = re.search(r"^\s*NELM\s*=\s*(\d+)", inc, re.M)
            rec["nelm"] = int(m.group(1)) if m else 0
        except Exception:
            pass
        out.append(rec)
except Exception:
    pass
# 节点故障：会离开 RUNNING，挂死扫描看不到；用 sacct 补最近 1h 的 NODE_FAIL
try:
    r2 = subprocess.run(["sacct", "-u", u, "-n", "-P", "-X",
                         "-o", "JobID,State,WorkDir", "--starttime", "now-3600"],
                        capture_output=True, text=True, timeout=60)
    for ln in r2.stdout.splitlines():
        p = ln.split("|")
        if len(p) < 3 or "NODE_FAIL" not in p[1].upper() or not p[2].strip():
            continue
        wd = p[2].strip()
        if any(x["wd"] == wd for x in out):
            continue
        out.append({"jobid": p[0].strip(), "wd": wd, "running": False,
                    "age": 0, "bytes": 0, "lines": 0, "last3": [],
                    "err_node": True, "err_disk": False,
                    "algo": "", "amix": False, "nelm": 0})
except Exception:
    pass
print(json.dumps(out))
'''
    b64 = base64.b64encode(code.encode()).decode()
    return run_remote(cfg, "echo %s | base64 -d | python3" % b64, host=host)

# ===== _material_of_workdir (原 L4502-L4509) =====
def _material_of_workdir(data, wdir):
    '''按远端工作目录反查材料 dict（用于 log_action 写项目日志）。'''
    for t in data.get("types", []):
        for m in t.get("materials", []):
            mp = m.get("path") or ""
            if mp and (wdir == mp or wdir.startswith(mp.rstrip(os.sep) + os.sep)):
                return m
    return None

# ===== _hung_scf_rms_trend (原 L4512-L4527) =====
def _hung_scf_rms_trend(last3):
    '''从 OSZICAR 末尾判断 SCF 是否还在推进：返回 True=还在降（慢但活着）。
    取最后 2 个 SCF 迭代行（DAV/CGA/RMM/EDDAV/DIIS/BLK）的最后一个数值（rms），
    rms 在降 = 自洽还在收敛，只是慢 → 不判挂死；rms 不降/涨 = 空转。'''
    vals = []
    for ln in (last3 or []):
        if re.match(r"\s*(DAV|CGA|RMM|EDDAV|DIIS|BLK)\s*:", ln):
            nums = re.findall(r"[-+]?\d*\.?\d+[eE][-+]?\d+", ln)
            if nums:
                try:
                    vals.append(float(nums[-1]))
                except ValueError:
                    pass
    if len(vals) < 2:
        return False
    return vals[-1] < vals[-2]

# ===== _hung_incar_fix (原 L4530-L4577) =====
def _hung_incar_fix(cfg, workdir, level):
    from tfpkg import run_remote
    '''远端升级 INCAR 抗 SCF 空转（幂等 + 原子写 + 备份）：
    level=1：补 AMIX=0.1 / BMIX=0.0001（精细混合）
    level=2：再改 ALGO=All（Davidson）；INCAR 没写 ALGO 行时补上（默认 Normal 也要显式 All）
    顺手把 NELM 提到至少 200（ALGO=All 迭代更贵，防撞 NELM）。
    写盘：先备份 INCAR → INCAR.bak.<ts>，再写 INCAR.tmp，os.replace 原子替换。
    返回 (rc, changed_list)。'''
    code = r'''
import os, re, time, json
p = os.path.join(@WDIR@, "INCAR")
s = open(p).read()
changed = []
if @LEVEL@ >= 1 and not re.search(r"^\s*AMIX\s*=", s, re.M):
    s = s.rstrip("\n") + "\nAMIX   = 0.1\nBMIX   = 0.0001\n"
    changed.append("AMIX/BMIX")
if @LEVEL@ >= 2:
    m = re.search(r"^\s*ALGO\s*=\s*(\S+)", s, re.M)
    if m:
        if m.group(1).upper() != "ALL":
            s = re.sub(r"^\s*ALGO\s*=\s*\S+", "ALGO   = All", s, count=1, flags=re.M)
            changed.append("ALGO=All")
    else:
        s = s.rstrip("\n") + "\nALGO   = All\n"
        changed.append("ALGO=All(新增)")
m = re.search(r"^\s*NELM\s*=\s*(\d+)", s, re.M)
if m and int(m.group(1)) < 200:
    s = re.sub(r"^\s*NELM\s*=\s*\d+", "NELM   = 200", s, count=1, flags=re.M)
    changed.append("NELM=200")
if changed:
    bak = p + ".bak." + time.strftime("%Y%m%d%H%M%S")
    try:
        os.rename(p, bak)
    except OSError:
        pass
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(s)
    os.replace(tmp, p)
print(json.dumps(changed))
'''
    code = code.replace("@WDIR@", json.dumps(workdir)).replace("@LEVEL@", str(level))
    b64 = base64.b64encode(code.encode()).decode()
    rc, out = run_remote(cfg, "echo %s | base64 -d | python3" % b64)
    try:
        changed = json.loads(out or "[]")
    except ValueError:
        changed = []
    return rc, changed

# ===== _hung_scancel_wait (原 L4580-L4593) =====
def _hung_scancel_wait(cfg, jobid, timeout=90):
    from tfpkg import run_remote
    '''scancel 后轮询 squeue 直到作业消失（避免旧 VASP 进程还在写文件时我们动文件）。
    返回 (ok, msg)。'''
    rc, _ = run_remote(cfg, "scancel %s" % jobid)
    if rc != 0:
        return False, "scancel rc=%s" % rc
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        _rc, o = run_remote(cfg, "squeue -h -j %s -o '%%T' 2>/dev/null" % jobid)
        if _rc != 0 or not (o or "").strip():
            return True, ""
        _t.sleep(5)
    return False, "scancel 后 %ds 作业仍未退出" % timeout

# ===== _hung_resume (原 L4596-L4612) =====
def _hung_resume(cfg, wdir):
    from tfpkg import run_remote
    '''从 CONTCAR 续跑（数据安全版）：
    先校验 CONTCAR 完整（>=8 行、原子数行与 POSCAR 一致），通过才 cp CONTCAR POSCAR，
    否则保留原 POSCAR 直接重跑并告警（IO 异常时 CONTCAR 可能是截断的，不能覆盖好 POSCAR）；
    备份旧输出为 *.hung，清理二进制残留，重新 sbatch。返回 (rc, msg)。'''
    lines = (
        "cd %s && mv -f OUTCAR OUTCAR.hung 2>/dev/null; "
        "mv -f OSZICAR OSZICAR.hung 2>/dev/null; "
        "rm -f vaspout.* XDATCAR WAVECAR 2>/dev/null; "
        "if [ -f CONTCAR ] && [ $(wc -l < CONTCAR) -ge 8 ] && "
        "[ \"$(awk 'NR==7{s=0;for(i=1;i<=NF;i++)s+=$i;print s}' CONTCAR)\" = \"$(awk 'NR==7{s=0;for(i=1;i<=NF;i++)s+=$i;print s}' POSCAR)\" ]; then "
        "  cp CONTCAR POSCAR; "
        "else "
        "  echo 'WARN: CONTCAR 缺失/不完整，保留原 POSCAR 直接重跑'; "
        "fi; "
        "sbatch submit.sh 2>&1" % shlex.quote(wdir))
    return run_remote(cfg, lines)

# ===== auto_recover_hung (原 L4615-L4773) =====
def auto_recover_hung(cfg, data):
    from tfpkg import log_action
    '''v1.11 挂死作业自动恢复（watch 每轮调用一次）。

    检测用【进度指纹】而不是单次年龄：每轮记录每个 RUNNING 作业的
    (OUTCAR 字节数, OSZICAR 行数)。指纹连续 hang_min_stale_rounds 轮不变
    且 输出年龄 >= hang_stale_secs 才算挂死——指纹在涨 = 活着，放它继续算，
    只有完全不动才是卡死。SCF 迭代行 rms 还在降 = 慢但活着（长单步），不判。

    诊断 + 处理：
      - err_disk（queue.err 报 No space/quota）：磁盘满 → 只告警不重跑
        （满盘上 mv/cp/写 INCAR 都会失败或写坏，重跑必死）。
      - err_node（queue.err 或 sacct 报 NODE_FAIL）：节点故障 → 直接重跑。
      - scf（OSZICAR 尾是 SCF 迭代行且 rms 不再降）：SCF 空转/尾部卡死 →
        hang_fix_scf 时自动升级 INCAR（补 AMIX/BMIX → ALGO=All → NELM>=200），
        再从 CONTCAR 续跑重交。
      - unknown：直接重跑。
    恢复动作：scancel → 轮询等作业退出 → 校验 CONTCAR 再续跑 → 重交。
    升级 INCAR 后给作业 hang_grace_rounds 轮宽限期（AMIX 降低使 SCF 变慢，
    避免因变慢被再次误判挂死）。

    hang_dry_run: true 时只打印判定不动手（观察期用，安全）。
    恢复次数受 hang_max_retries 限制（计数存 <配置目录>/.tf_hung.json），超限只告警。

    配置（全局 tf.yaml，可被 技能/项目 覆盖）：
      hang_check / hang_stale_secs / hang_max_retries / hang_fix_scf /
      hang_min_stale_rounds（默认 2）/ hang_grace_rounds（默认 3）/ hang_dry_run（默认 false）
    '''
    if not _hung_enabled(cfg):
        return
    stale_secs = int(_hung_cfg(cfg, None, None, "hang_stale_secs", 5400))
    fix_scf = bool(_hung_cfg(cfg, None, None, "hang_fix_scf", True))
    min_rounds = int(_hung_cfg(cfg, None, None, "hang_min_stale_rounds", 2))
    grace = int(_hung_cfg(cfg, None, None, "hang_grace_rounds", 3))
    dry = bool(_hung_cfg(cfg, None, None, "hang_dry_run", False))
    rc, out = _hung_scan(cfg)
    if rc != 0:
        print("hang-check：远端扫描失败：%s" % (out or "")[:200])
        return
    try:
        jobs = json.loads(out or "[]")
    except ValueError:
        print("hang-check：远端扫描输出解析失败：%s" % (out or "")[:200])
        return
    state = _hung_state_load(cfg)
    _now = time.strftime("%H:%M:%S")
    for rec in jobs:
        wd = rec.get("wd")
        if not wd:
            continue
        st = state.setdefault(wd, {})
        if not isinstance(st, dict):        # 旧格式 {wd: int} 迁移
            st = state[wd] = {"recovered": st if isinstance(st, int) else 0}
        # 宽限期：升级 INCAR 后 SCF 变慢，给几轮缓冲防误判
        if st.get("grace", 0) > 0:
            st["grace"] = st["grace"] - 1
            continue
        if not rec.get("running"):
            # sacct 补的 NODE_FAIL（已离开队列）：直接走恢复（无需指纹）
            m = _material_of_workdir(data, wd)
            mst = (m.get("ps") or {}).get("setting") if m else None
            maxr = int(_hung_cfg(cfg, None, mst, "hang_max_retries", 2))
            n = st.get("recovered", 0)
            if n >= maxr:
                print("[%s] hang：job %s（%s）NODE_FAIL 已恢复 %d/%d 次，停止重试。"
                      % (_now, rec.get("jobid"), wd, n, maxr))
                continue
            if dry:
                print("[%s] hang[干跑]：job %s（%s）NODE_FAIL，将重跑。"
                      % (_now, rec.get("jobid"), wd))
                continue
            ok, msg = _hung_scancel_wait(cfg, rec.get("jobid"))
            if not ok and "scancel rc" not in msg:
                print("[%s] hang：job %s 取消失败：%s" % (_now, rec.get("jobid"), msg))
                continue
            _rc3, o3 = _hung_resume(cfg, wd)
            st["recovered"] = n + 1
            st["grace"] = grace
            print("[%s] hang 自动恢复：job %s（%s）NODE_FAIL（第 %d/%d 次）→ %s"
                  % (_now, rec.get("jobid"), wd, n + 1, maxr,
                     (o3 or "").strip() or "已重交"))
            if m:
                log_action(m, "hang 自动恢复 job=%s NODE_FAIL（第 %d/%d 次）→ %s"
                           % (rec.get("jobid"), n + 1, maxr, (o3 or "").strip() or "已重交"))
            continue
        # ---- RUNNING 作业：进度指纹 ----
        age = rec.get("age")
        if age is None:
            continue
        fp = (rec.get("bytes") or 0, rec.get("lines") or 0)
        if (st.get("bytes") == fp[0] and st.get("lines") == fp[1]
                and st.get("bytes") is not None):
            st["unchanged"] = st.get("unchanged", 0) + 1
        else:
            st["unchanged"] = 0
        st["bytes"], st["lines"] = fp[0], fp[1]
        if age < stale_secs or st.get("unchanged", 0) < min_rounds:
            continue
        # 长单步 SCF 还在降 rms = 慢但活着，放它继续算
        if _hung_scf_rms_trend(rec.get("last3")):
            st["unchanged"] = 0
            continue
        m = _material_of_workdir(data, wd)
        mst = (m.get("ps") or {}).get("setting") if m else None
        maxr = int(_hung_cfg(cfg, None, mst, "hang_max_retries", 2))
        n = st.get("recovered", 0)
        if n >= maxr:
            print("[%s] hang：job %s（%s）指纹 %d 轮不变/无输出 %.0fs，"
                  "已恢复 %d/%d 次，停止重试，需人工/AI 介入。"
                  % (_now, rec.get("jobid"), wd, st.get("unchanged", 0), age, n, maxr))
            continue
        cause, incar_fix = "unknown", None
        if rec.get("err_disk"):
            cause = "disk"
        elif rec.get("err_node"):
            cause = "node"
        elif _hung_scf_rms_trend(rec.get("last3")) is False and rec.get("last3") and re.match(
                r"\s*(DAV|CGA|RMM|EDDAV|DIIS|BLK)\s*:", rec["last3"][-1]):
            cause = "scf"
            if fix_scf:
                if not rec.get("amix"):
                    incar_fix = 1
                elif str(rec.get("algo", "")).upper() != "ALL":
                    incar_fix = 2
                else:
                    incar_fix = 0
        if dry:
            print("[%s] hang[干跑]：job %s（%s）指纹 %d 轮不变/无输出 %.0fs，"
                  "原因=%s，将执行 %s。"
                  % (_now, rec.get("jobid"), wd, st.get("unchanged", 0), age, cause,
                     ("INCAR升级+重跑" if incar_fix else
                      ("只告警不重跑" if cause == "disk" else "重跑"))))
            continue
        if cause == "disk":
            print("[%s] hang：job %s（%s）磁盘满告警（No space），不重跑，请清理磁盘。"
                  % (_now, rec.get("jobid"), wd))
            continue
        ok, msg = _hung_scancel_wait(cfg, rec.get("jobid"))
        if not ok:
            print("[%s] hang：job %s 取消/等待退出失败：%s" % (_now, rec.get("jobid"), msg))
            continue
        fix_txt = ""
        if incar_fix is not None and incar_fix > 0:
            rc2, changed = _hung_incar_fix(cfg, wd, incar_fix)
            if rc2 == 0 and changed:
                fix_txt = " INCAR升级(%s)" % ",".join(changed)
            elif rc2 != 0:
                print("[%s] hang：job %s INCAR 升级失败（rc=%s），继续原样重跑。"
                      % (_now, rec.get("jobid"), rc2))
        _rc3, o3 = _hung_resume(cfg, wd)
        st["recovered"] = n + 1
        st["grace"] = grace
        nid = (o3 or "").strip() or "已重交"
        print("[%s] hang 自动恢复：job %s（%s）无输出 %.0fs（第 %d/%d 次）"
              "原因=%s%s → %s"
              % (_now, rec.get("jobid"), wd, age, n + 1, maxr, cause, fix_txt, nid))
        if m:
            log_action(m, "hang 自动恢复 job=%s（无输出 %.0fs，第 %d/%d 次，原因=%s%s）→ %s"
                       % (rec.get("jobid"), age, n + 1, maxr, cause, fix_txt, nid))
    _hung_state_save(cfg, state)

# ===== 来自 14_init.py =====
# -*- coding: utf-8 -*-
# 14_init —— 项目初始化（init / init_skill / yaml block 编辑）
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L5272  cmd_init
#   L5423  _scan_root_dirs
#   L5441  resolve_mat_dir
#   L5490  skill_keys
#   L5497  _init_one
#   L5541  _scope_to_material
#   L5559  _init_one_skill
#   L5795  _yaml_type_block_ensure
#   L5830  _yaml_type_block_set
#   L5878  _yaml_type_block_remove

# ===== cmd_init (原 L5272-L5417) =====
def cmd_init(cfg, types, proj, name=None, tt=None, force=False, yes=False):
    from tfpkg import scan_project_configs
    """初始化项目配置。
    -p 指定材料 → 只初始化这些材料（多个用逗号分隔，如 -p Mg2C60,Mo2S3）；
    不带 -p → 当前目录下所有项目批量初始化（cwd 下一层就是材料目录时，
    cwd 本身作为一个项目）。位置参数可指定项目名（tf init 名字）。
    不带 -tt 时对【全部技能】各建一套 project_setting；动手前先列计划并确认（-y 跳过）。"""
    _keys = skill_keys(cfg, tt)
    if not _keys:
        print("错误：tf.yaml 里没有定义任何技能（task_types 为空）。")
        return 1
    if not yes and not tt:   # 明确指定了 -tt 就是明确选择，不再确认
        print("tf init 将为下列技能各建一套项目配置：%s" % "、".join(_keys))
        print("范围：%s" % (("材料 " + "、".join(
            x.strip() for x in str(proj).split(",") if x.strip())) if proj
            else "当前目录下所有材料（%s）" % os.getcwd()))
        print("init 只在本地生成配置和模板，不连超算、不提交任何计算；")
        print("要开算是之后的 tf start。已存在的文件不覆盖（除非加 -f）。")
        print("（只想初始化其中一个技能就加 -tt，如 tf -tt band init）")
        try:
            ans = input("继续？ [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    # 预扫一次所有项目配置名（tf_*.yaml）→ {项目名: 路径}，批量 init 时下发给
    # _init_one/_init_one_skill 做 O(1) 查重。否则每个材料都全树重扫一遍，
    # 大体系（成千上万个材料）会退化成 O(N²)，极其缓慢。
    known_names = {n: p for n, p, _d in scan_project_configs(
        cfg.get("project_roots") or [cfg.get("_config_dir")])}
    if proj:
        # v1.9.8：-p 支持逗号分隔的多个材料（-p Mg2C60,Mo2S3）；定位交给
        # resolve_mat_dir，正常发现失败时会扫盘兜底。
        wants = [x.strip() for x in str(proj).split(",") if x.strip()]
        fails = 0
        _targets = []                       # [(want, lpath)]
        for w in wants:
            lp = resolve_mat_dir(cfg, types, tt, w)
            if not lp:
                print("错误：找不到材料 %s —— 当前目录、project_roots 和各技能的 "
                      "local_root 下都没有同名的、带 POSCAR 的目录。" % w)
                fails += 1
                continue
            _targets.append((w, lp))
        _nm = name if len(wants) == 1 else None
        if len(_targets) == 1:              # 单材料：保留原串行路径（name 命名 + 完整输出）
            fails += _init_one(cfg, types, _targets[0][1], _nm,
                               tt=tt, force=force, known_names=known_names)
        elif _targets:                      # 多材料：并行 init（与不带 -p 的批量同策略）
            import concurrent.futures as _cf
            _nw = int(os.environ.get("TF_INIT_WORKERS", "16") or 16)
            _real_out = sys.stdout
            _failed = []

            def _work(item):
                _w, _lp = item
                return _w, _lp, _init_one(cfg, types, _lp, None, tt=tt,
                                          force=force, known_names=known_names)

            with _cf.ThreadPoolExecutor(max_workers=_nw) as _ex:
                _futs = [_ex.submit(_work, it) for it in _targets]
                try:
                    sys.stdout = open(os.devnull, "w")
                    _n = 0
                    for _f in _cf.as_completed(_futs):
                        _w, _lp, _rc = _f.result()
                        _n += 1
                        if _rc:
                            _failed.append((_w, _lp))
                        if _n % 200 == 0 or _n == len(_targets):
                            print("  init %d/%d（失败 %d）"
                                  % (_n, len(_targets), len(_failed)),
                                  file=sys.stderr, flush=True)
                finally:
                    sys.stdout = _real_out
            for _w, _lp in _failed:
                print("== %s == 失败，串行重跑以显示报错：" % _w)
                fails += _init_one(cfg, types, _lp, None, tt=tt, force=force,
                                   known_names=known_names)
        return fails
    import glob as _glob
    cwd = os.getcwd()
    if os.path.isfile(os.path.join(cwd, "POSCAR")):
        return _init_one(cfg, types, cwd, name, tt=tt, force=force,
                         known_names=known_names)  # cwd 本身是材料目录
    matdirs = sorted({os.path.dirname(p)
                      for pat in ("*/POSCAR", "*/*/POSCAR", "*/*/*/POSCAR")
                      for p in _glob.glob(os.path.join(cwd, pat))})
    if not matdirs:
        print("错误：当前目录下没有发现材料目录（含 POSCAR）。")
        return 1
    # v1.5：批量 init 只初始化 project_roots 覆盖范围内的目录——在家目录等
    # 大范围目录下运行时，其他工作流的材料（不在 tf 管理内）跳过不误建。
    roots = [os.path.realpath(os.path.expanduser(r))
             for r in (cfg.get("project_roots") or [])]
    def _managed(d):
        rd = os.path.realpath(d)
        return any(rd == r or rd.startswith(r + os.sep) for r in roots)
    managed, skipped = [], []
    for d in matdirs:
        (managed if _managed(d) else skipped).append(d)
    for d in skipped:
        print("跳过 %s（不在 tf.yaml 的 project_roots 内；要纳入管理，"
              "先把它的根目录加进 project_roots）" % os.path.relpath(d, cwd))
    if not managed:
        print("错误：发现的材料目录都不在 project_roots 内，未初始化任何项目。")
        return 1
    matdirs = managed
    fails, done = 0, 0
    # v1.2/v1.9.4：已初始化项目也进 _init_one 补齐缺的技能段（批量挂新技能）。
    # 大体系并行 init：每个材料只写自己的 project_setting，互不依赖，用线程池
    # 并行纯本地 I/O（配合上面的 O(1) 查重，整体 O(N)）。循环期间 stdout 静默
    # 以免成千上万个材料刷屏交错，进度走 stderr；失败的材料结束后串行重跑回显。
    import concurrent.futures as _cf
    _nw = int(os.environ.get("TF_INIT_WORKERS", "16") or 16)
    _real_out = sys.stdout
    _failed = []

    def _init_work(d):
        return d, _init_one(cfg, types, d, None, tt=tt, force=force,
                            known_names=known_names)

    with _cf.ThreadPoolExecutor(max_workers=_nw) as _ex:
        _futs = [_ex.submit(_init_work, d) for d in matdirs]
        try:
            sys.stdout = open(os.devnull, "w")
            _n = 0
            for _f in _cf.as_completed(_futs):
                _d, _rc = _f.result()
                _n += 1
                if _rc:
                    _failed.append(_d)
                else:
                    done += 1
                if _n % 200 == 0 or _n == len(matdirs):
                    print("  init %d/%d（失败 %d）" % (_n, len(matdirs), len(_failed)),
                          file=sys.stderr, flush=True)
        finally:
            sys.stdout = _real_out
    for d in _failed:
        print("== %s == 失败，串行重跑以显示报错：" % os.path.relpath(d, cwd))
        fails += _init_one(cfg, types, d, None, tt=tt, force=force,
                           known_names=known_names)
    if not fails:
        print("材料初始化就绪（新 %d 个）。tf 查看状态，tf start 全部开始。" % done)
    return fails

# ===== _scan_root_dirs (原 L5423-L5435) =====
def _scan_root_dirs(root):
    from tfpkg import _MAT_DIR_CACHE
    """扫一个根下所有带 POSCAR 的目录（缓存）。批量 auto/clean 反复按名解析时，
    串行逐材料 glob 整个 project_roots（/mnt/d 这种 9p 挂载极慢）会退化成 O(N*树)，
    这里扫一次缓存、后面 O(1) 查表。"""
    if root in _MAT_DIR_CACHE:
        return _MAT_DIR_CACHE[root]
    out = []
    for pat in ("*/POSCAR", "*/*/POSCAR", "*/*/*/POSCAR"):
        for pp in sorted(glob.glob(os.path.join(root, pat))):
            d = os.path.dirname(pp)
            out.append((os.path.relpath(d, root), os.path.basename(d), d))
    _MAT_DIR_CACHE[root] = out
    return out

# ===== resolve_mat_dir (原 L5441-L5487) =====
def resolve_mat_dir(cfg, types, tt, want, cwd=None):
    from tfpkg import _RESOLVE_DISC_CACHE, discover_local, get_types
    """按名字定位材料的本地目录，找不到返回 None。
    先走正常发现（local_root -> discover_local）；再扫盘兜底——clean 删光
    project_setting 后 local_root 也跟着没了，只能靠扫盘自举回来。
    批量 -p 时同一 local_root 的 discover_local 结果做进程内缓存，避免
    M 材料 × T 类型 重复扫盘。"""
    tries = [types or []]
    if tt:
        try:
            tries.append(get_types(cfg, tt=None, quiet=True))
        except SystemExit:
            pass
    for tlist in tries:
        for t0 in tlist:
            if not t0.get("local_root"):
                continue
            _key = os.path.realpath(os.path.expanduser(t0["local_root"]))
            mats = _RESOLVE_DISC_CACHE.get(_key)
            if mats is None:
                try:
                    _r, mats = discover_local(t0["local_root"])
                except Exception:
                    _RESOLVE_DISC_CACHE[_key] = []   # 发现失败也缓存空，避免重试
                    continue
                _RESOLVE_DISC_CACHE[_key] = mats
            for m in mats:
                if m["name"] == want or os.path.basename(m["name"]) == want:
                    return m["lpath"]
    roots = [cwd or os.getcwd()]
    for r in (cfg.get("project_roots") or []):
        roots.append(str(r))
    for t0 in (types or []):
        if t0.get("local_root"):
            roots.append(t0["local_root"])
    seen = set()
    for r in roots:
        r = os.path.abspath(os.path.expanduser(str(r)))
        if r in seen or not os.path.isdir(r):
            continue
        seen.add(r)
        if os.path.basename(r) == want and os.path.isfile(
                os.path.join(r, "POSCAR")):
            return r
        for rel, base, d in _scan_root_dirs(r):
            if rel == want or base == want:
                return d
    return None

# ===== skill_keys (原 L5490-L5494) =====
def skill_keys(cfg, tt=None):
    """要初始化哪些技能。给了 -tt 就只有它；否则 tf.yaml 里定义的全部技能。"""
    if tt:
        return [tt]
    return [k for k in (cfg.get("task_types") or {}) if k]

# ===== _init_one (原 L5497-L5538) =====
def _init_one(cfg, types, target, name=None, tt=None, force=False, brief=None,
              known_names=None):
    """给 target 材料目录初始化【全部技能】（或 -tt 指定的那一个）。
    brief=None 时自动判断：一次建多个技能就每个技能只打一行摘要，避免刷屏。
    known_names：批量 init 预扫好的 {项目名: tf_*.yaml 路径}，用于 O(1) 查重
    （避免每个材料都全树重扫一遍；大体系下这是 O(N²) → O(N) 的关键）。"""
    import contextlib as _ctx
    import io as _io
    keys = skill_keys(cfg, tt)
    if brief is None:
        brief = len(keys) > 1
    fails = 0
    for k in keys:
        if not brief:
            fails += _init_one_skill(cfg, types, target, name, tt=k, force=force,
                                     known_names=known_names)
            continue
        buf = _io.StringIO()
        with _ctx.redirect_stdout(buf):
            rc = _init_one_skill(cfg, types, target, name, tt=k, force=force,
                                 known_names=known_names)
        fails += rc
        out = buf.getvalue()
        n_new = out.count("已生成 ")
        n_skip = out.count("已存在，跳过")
        mt = re.search(r"已(?:复制|覆盖) (\d+) 个模板/配置", out)
        if n_new:
            msg = "新建 %d 个配置" % n_new
        elif n_skip:
            msg = "已存在，未改动"
        else:
            msg = "就绪"
        if mt:
            msg += "，模板 %s 个" % mt.group(1)
        print("  %-9s %s" % (k, msg))
        for ln in out.splitlines():      # 错误和需要人工处理的提示照常显示
            if ln.startswith(("错误", "提示：skill_dir")):
                print("    " + ln)
    if brief:
        print("  → 配置在 %s/<技能>/project_setting/（改这里只影响本材料）"
              % os.path.basename(os.path.abspath(target)))
    return fails

# ===== _scope_to_material (原 L5541-L5556) =====
def _scope_to_material(content, tkey):
    """kls7-scope：skill_subdir 布局下把项目配置的发现范围锁到本材料。

    <材料>/<技能>/project_setting 的缺省 local_root 是"材料的父目录"，
    discover_local 会把同级所有带 POSCAR 的目录都扫成本技能的材料 ——
    在一个材料下 init 某技能，兄弟材料全被拉进该技能的表。
    显式写 local_root: ".."（相对 project_setting 的父目录 = <材料>/<技能>）
    即指向材料目录本身，只发现这一个材料。要整批管就到上级目录 tf init。
    """
    if re.search(r"(?m)^\s+local_root:", content):
        return content
    m = re.search(r"(?m)^(\s*)%s:\s*$" % re.escape(str(tkey or "")), content)
    if not m:
        return content
    line = '%s  local_root: ".."   # 只发现本材料；整批管请到上级目录 tf init\n' % m.group(1)
    return content[:m.end()] + "\n" + line + content[m.end() + 1:]

# ===== _init_one_skill (原 L5559-L5792) =====
def _init_one_skill(cfg, types, target, name=None, tt=None, force=False,
                    known_names=None):
    from tfpkg import DEFAULT_HPC_SETTING, DEFAULT_PROJECT_CONFIG, DEFAULT_PROJECT_SETTING, _PKG_ROOT, _load_yaml_file, _same_file, _skill_asset_dirs, pkg_setting_path, scan_project_configs
    """在 target 目录生成 project_setting/（tf_<项目名>.yaml + setting.yaml +
    hpc.yaml + 映射模板）。已存在的文件不覆盖；项目配置名全局唯一，重复即报错。
    known_names：批量 init 预扫好的 {项目名: tf_*.yaml 路径}，传入则查重 O(1)
    （不再每个材料全树重扫），新建成功后同步加入该集合。"""
    cands = [x for x in types if x.get("local_root")]
    if tt:   # v1.9.4：锁定到该技能，否则总是拿到排在最前面的 band
        cands = [x for x in cands if x.get("key") == tt]
    t = next((tt for tt in cands if not tt.get("_from")), None)
    tkey = (t or {}).get("key")
    if t is None:  # 模板字段优先继承全局骨架（无 local_root 的主定义）
        # v1.2：-tt 过滤后 types 只剩目标类型的段时，骨架也要锁定该类型
        # （否则 tf -tt elastic init 会拿到排在前面的 band 骨架，追加错段）
        want = {tt2.get("key") for tt2 in types} - {None}
        # v1.3.2：types 为空（该技能还没有任何项目段）时 want 也空，
        # 锁定会失效退回拿 band 骨架——用 -tt 的 key 兜底
        if tt:
            want = {tt}
        for k, tc in (cfg.get("task_types") or {}).items():
            if want and k not in want:
                continue
            if tc and not tc.get("local_root"):
                t, tkey = dict(tc), k
                break
    if t is None and cands:
        t = cands[0]
        tkey = tkey or t.get("key")
    # v1.7：开 skill_subdir 时 project_setting 进技能子目录
    # （材料/<技能>/project_setting），每技能一套、完全自包含；
    # 配置名带技能后缀保证全局唯一（scan_project_configs 要求名字唯一）。
    _sub = (str((t or {}).get("dir_name") or tkey)
            if (t and t.get("skill_subdir")) else None)
    ps = (os.path.join(target, _sub, "project_setting") if _sub
          else os.path.join(target, "project_setting"))
    os.makedirs(ps, exist_ok=True)
    # 项目配置 tf_<项目名>.yaml（命名全局唯一，禁止重复）
    pname = name or re.sub(r"\W+", "_", os.path.basename(os.path.abspath(target)))
    if _sub and tkey and not pname.endswith("_" + str(tkey)):
        pname = "%s_%s" % (pname, tkey)   # v1.7：技能级配置名带技能后缀
    f0 = os.path.join(ps, "tf_%s.yaml" % pname)
    if os.path.exists(f0):
        # v1.2：配置已存在且带了 -tt——缺该类型段就追加（已有项目挂新技能，
        # 如 band 项目追加 elastic），不再只"跳过"。空段 = 字段全继承全局骨架。
        existing = _load_yaml_file(f0)
        if tkey and tkey not in (existing.get("task_types") or {}):
            with open(f0, encoding="utf-8") as f:
                lines = f.readlines()
            tt_idx = next((i for i, l in enumerate(lines)
                           if l.startswith("task_types:")), None)
            if tt_idx is not None:
                end = len(lines)
                for i in range(tt_idx + 1, len(lines)):
                    l = lines[i]
                    if l.strip() and not l.startswith((" ", "\t", "#")):
                        end = i
                        break
                lines.insert(end, "  %s:\n" % tkey)
                with open(f0, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                print("已追加 %s 段 → %s（字段全继承全局骨架，可按项目改）"
                      % (tkey, f0))
            else:
                print("已存在，跳过 %s（未找到 task_types 块，请手工加 %s 段）"
                      % (f0, tkey))
        else:
            print("已存在，跳过 %s" % f0)
    else:
        if known_names is not None:
            # 批量 init 预扫过的名字表 → O(1) 查重，避免每个材料全树重扫
            if pname in known_names:
                print("错误：项目配置名 tf_%s.yaml 已被 %s 占用，"
                      "请换个名字（tf init <名字>）。" % (pname, known_names[pname]))
                return 1
        else:
            roots = cfg.get("project_roots") or [cfg.get("_config_dir")]
            for n2, p2, _ in scan_project_configs(roots):
                if n2 == pname:
                    print("错误：项目配置名 tf_%s.yaml 已被 %s 占用，"
                          "请换个名字（tf init <名字>）。" % (pname, p2))
                    return 1
        src = pkg_setting_path("tf_default.yaml")
        content = None
        if src:
            with open(src, encoding="utf-8") as f:
                content = f.read()
        if not content:
            content = DEFAULT_PROJECT_CONFIG
        if t and t.get("work_dir"):
            content = re.sub(r"(?m)^(\s*)work_dir:.*$",
                             r"\1work_dir: " + t["work_dir"], content, count=1)
        # v1.9.5：模板里写死的 desc 会盖掉 skill.yaml 的（elastic/ke 也显示"能带计算"）。
        # 直接删掉这一行，desc 一律由技能自己声明。
        content = re.sub(r"(?m)^\s*desc:.*\n", "", content, count=1)
        if tkey:  # v1.3：类型名同步成继承骨架的真实 key——模板可能还是旧名
                  # （如全局已 bd→band，模板没跟上），直接生成会断链报错
            mk = re.search(r"(?m)^(task_types:\s*\n\s*)[^\s:#]+:", content)
            if mk:
                content = (content[:mk.start()] + mk.group(1) + tkey + ":"
                           + content[mk.end():])
        if _sub:   # kls7-scope：技能子目录布局 = 一材料一项目
            content = _scope_to_material(content, tkey)
        with open(f0, "w", encoding="utf-8") as f:
            f.write(content)
        if known_names is not None:
            known_names[pname] = f0   # 批量 init：新名字同步进预扫集合，后续材料继续 O(1) 查重
        print("已生成 %s（项目配置：步骤/超算/路径按项目改它）" % f0)
    f1 = os.path.join(ps, "setting.yaml")
    if os.path.exists(f1):
        print("已存在，跳过 %s" % f1)
    else:
        with open(f1, "w", encoding="utf-8") as f:
            f.write(DEFAULT_PROJECT_SETTING)
        print("已生成 %s" % f1)
    f2 = os.path.join(ps, "hpc.yaml")
    if os.path.exists(f2):
        print("已存在，跳过 %s" % f2)
    else:
        hpc_name = (t or {}).get("hpc") or "jzzn"
        src = pkg_setting_path(hpc_name + ".yaml")
        if src:
            shutil.copyfile(src, f2)
        else:
            with open(f2, "w", encoding="utf-8") as f:
                f.write(DEFAULT_HPC_SETTING.replace("name: jzzn",
                                                    "name: " + hpc_name))
        print("已生成 %s" % f2)
    # 按 hpc.yaml 的 template_map 把映射到的提交模板复制进项目（可再按项目改）
    hpc_cfg = _load_yaml_file(f2)
    sd = (t or {}).get("skill_dir")
    if sd and not os.path.isabs(sd):
        # v1.3：相对路径先按配置目录找，找不到再按软件根找
        # （tf.yaml 放 setting/ 时，skill/ 在软件根下，只按配置目录会找偏）
        pkg_root = _PKG_ROOT
        for base in (cfg.get("_config_dir"), pkg_root):
            cand = os.path.normpath(os.path.join(base or ".", sd))
            if os.path.isdir(cand):
                sd = cand
                break
        else:
            sd = os.path.normpath(os.path.join(cfg.get("_config_dir") or ".", sd))
    # v1.3：只复制该技能实际引用的模板——步骤级 gen_need 都声明了的技能
    # （如 elastic 全程 std），按步骤清单并集复制；没声明的沿用全量（band）。
    # 否则 elastic init 会对用不到的 ncl 模板报"请手动放入"误导用户
    # v1.7：shared 布局改由下方"整套 templates/"复制统一负责，这里的平铺复制
    # 只在 per_step（无 templates/ 子文件夹）时才需要，避免 project_setting 根下
    # 与 templates/ 里重复放一份 submit_*。
    _need = set()
    for _sc in ((t or {}).get("steps") or []):
        _need.update(_sc.get("gen_need") or [])
    _tmpl_need = {x for x in _need if str(x).endswith(".tpl")} or None
    _shared_full = (str((t or {}).get("template_layout") or "shared").lower()
                    != "per_step") and bool(sd)
    for logical, real in ((hpc_cfg.get("template_map") or {}).items()
                          if not _shared_full else []):
        if _tmpl_need is not None and logical not in _tmpl_need:
            continue
        dst = os.path.join(ps, real)
        if os.path.exists(dst):
            print("已存在，跳过 %s" % dst)
            continue
        _layout = str((t or {}).get("template_layout") or "shared").lower()
        if _layout == "per_step":
            # 每步一套模板：不能复制到 project_setting（那里一份会盖住所有步骤）。
            # 模板留在 skill/<技能>/templates/<步骤名>/，要按项目改就放
            # 材料/<技能>/ 下（优先级仍高于技能目录）。
            continue
        srcf = None
        for _d in (_skill_asset_dirs(t or {}, {}, sd) if sd else []):
            _c = os.path.join(_d, real)
            if os.path.isfile(_c):
                srcf = _c
                break
        if srcf:
            shutil.copyfile(srcf, dst)
            print("已复制模板 %s" % dst)
        else:
            print("提示：skill_dir 里找不到 %s，请手动放入 %s" % (real, dst))
    # v1.7：把技能的整套 templates/（含 incar_*.tpl，template_map 未覆盖的也一并）
    # 复制进项目的 templates/ 子文件夹——按项目/材料手改只动这里，不影响其它材料。
    # 目的地：开 skill_subdir 时进 材料/<技能>/templates/，否则 project_setting/templates/。
    # find_asset 会优先读这里（v1.7 查找链已含 <技能>/templates 与 ps/templates）。
    # v1.9：把技能的整套 templates/ 递归复制进项目（保留 <步骤名>/ 子目录结构），
    # 含 *.tpl 与 *.conf。改这里只影响本项目/材料，不动 skill 库。
    if sd:
        _tsrc = os.path.join(sd, str((t or {}).get("template_dir") or "templates"))
        _tdst = os.path.join(ps, "templates")
        _copied = 0
        _jobs = []
        if os.path.isdir(_tsrc):
            for _dp, _dn, _fns in os.walk(_tsrc):
                _rel = os.path.relpath(_dp, _tsrc)
                _jobs.append((_dp, _tdst if _rel == "." else
                              os.path.join(_tdst, _rel), _fns))
        if os.path.isdir(sd):    # 技能根下平铺的老模板也收进 templates/
            _jobs.append((sd, _tdst, os.listdir(sd)))
        for _sdir, _ddir, _fns in _jobs:
            for _fn in sorted(_fns):
                if not _fn.endswith((".tpl", ".conf")):
                    continue
                _s, _d = os.path.join(_sdir, _fn), os.path.join(_ddir, _fn)
                if not os.path.isfile(_s):
                    continue
                if os.path.exists(_d):
                    if not force:
                        continue
                    if _same_file(_s, _d):
                        continue
                    shutil.copyfile(_d, _d + ".bak")   # v1.9：-f 覆盖前先备份
                    print("  备份 %s -> %s.bak" % (_d, os.path.basename(_d)))
                os.makedirs(_ddir, exist_ok=True)
                shutil.copyfile(_s, _d)
                _copied += 1
        if _copied:
            print("已%s %d 个模板/配置 -> %s/（按步骤分子目录；改这里只影响本材料）"
                  % ("覆盖" if force else "复制", _copied, _tdst))
        else:
            print("templates 就绪：%s/（%s）"
                  % (_tdst, "与 skill 出厂版一致" if force else
                     "已存在，未覆盖；要用 skill 出厂版刷新加 -f"))
    # v1.2：技能开 skill_subdir 时本地建技能子目录（本地镜像超算结构：
    # 材料/<技能>/{result,log} ↔ 超算 work/材料/<技能>/stepN）
    if (t or {}).get("skill_subdir"):
        sdname = str(t.get("dir_name") or tkey)
        sddir = os.path.join(target, sdname)
        if not os.path.isdir(sddir):
            os.makedirs(sddir, exist_ok=True)
            print("已创建技能目录 %s/（result/log 都在里面）" % sddir)
        print("提示：%s 要与其它技能用不同超算时，把 project_setting/hpc.yaml "
              "复制为 %s 再改字段即可（ssh_host/template_map/队列）"
              % (tkey, os.path.join(sddir, "hpc.yaml")))
    print("project_setting 就绪：%s（换超算改 hpc.yaml，调目录/结果改 setting.yaml）" % ps)
    return 0

# ===== _yaml_type_block_ensure (原 L5795-L5827) =====
def _yaml_type_block_ensure(path, tkey, kv_line):
    """确保项目配置 task_types.<tkey> 段内有 kv_line（如 "    skill_subdir: true"）。
    段缺失 → 追加新段；段存在且已有该键 → 不动。返回 True=有改动。"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    tt = next((i for i, l in enumerate(lines) if l.startswith("task_types:")),
              None)
    if tt is None:
        return False
    end = len(lines)   # task_types 块范围 [tt+1, end)
    for i in range(tt + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "\t", "#")):
            end = i
            break
    seg = next((i for i in range(tt + 1, end)
                if re.match(r"^  %s\s*:" % re.escape(tkey), lines[i])), None)
    if seg is None:
        lines.insert(end, "  %s:\n%s\n" % (tkey, kv_line))
    else:
        seg_end = end   # 段范围 [seg+1, seg_end)：≥4 空格缩进/注释/空行
        for i in range(seg + 1, end):
            if lines[i].strip() and re.match(r"^  [^ \t#]", lines[i]):
                seg_end = i
                break
        key = kv_line.split(":", 1)[0].strip()
        for i in range(seg + 1, seg_end):
            if re.match(r"^\s+%s\s*:" % re.escape(key), lines[i]):
                return False   # 已有该键
        lines.insert(seg + 1, kv_line + "\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True

# ===== _yaml_type_block_set (原 L5830-L5875) =====
def _yaml_type_block_set(path, tkey, key, value):
    """在项目配置 task_types.<tkey> 段内写 key: value（无则插入，有则改值），
    保留注释与其它键。返回 True=有改动。用于按需启用可选组（写 bandgap_hse: true）。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False
    tt = next((i for i, l in enumerate(lines) if l.startswith("task_types:")),
              None)
    if tt is None:
        return False
    end = len(lines)
    for i in range(tt + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "\t", "#")):
            end = i
            break
    seg = next((i for i in range(tt + 1, end)
                if re.match(r"^  %s\s*:" % re.escape(tkey), lines[i])), None)
    if seg is None:
        seg = end
        lines.insert(end, "  %s:\n" % tkey)
        end = seg + 1
    seg_end = end
    for i in range(seg + 1, end):
        if lines[i].strip() and re.match(r"^  [^ \t#]", lines[i]):
            seg_end = i
            break
    if value is True:
        vtxt = "true"
    elif value is False:
        vtxt = "false"
    else:
        vtxt = str(value)
    val_line = "    %s: %s\n" % (key, vtxt)
    for i in range(seg + 1, seg_end):
        if re.match(r"^\s+%s\s*:" % re.escape(key), lines[i]):
            lines[i] = val_line
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return True
    lines.insert(seg + 1, val_line)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True

# ===== _yaml_type_block_remove (原 L5878-L5905) =====
def _yaml_type_block_remove(path, tkey):
    """从项目配置 task_types 下删除 tkey 段（段头到下一个两空格键/块尾）。
    返回 True=有删除。与 _yaml_type_block_ensure 对称。"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    tt = next((i for i, l in enumerate(lines) if l.startswith("task_types:")),
              None)
    if tt is None:
        return False
    end = len(lines)
    for i in range(tt + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "\t", "#")):
            end = i
            break
    seg = next((i for i in range(tt + 1, end)
                if re.match(r"^  %s\s*:" % re.escape(tkey), lines[i])), None)
    if seg is None:
        return False
    seg_end = end
    for i in range(seg + 1, end):
        if lines[i].strip() and re.match(r"^  [^ \t#]", lines[i]):
            seg_end = i
            break
    del lines[seg:seg_end]
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True

# ===== 来自 15_hpc.py =====
# -*- coding: utf-8 -*-
# 15_hpc —— hpc / level / auto / adopt / migrate-subdir 命令
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L5908  _write_hpc_yaml
#   L5926  cmd_hpc
#   L6016  _list_pkg_clusters
#   L6049  _level_stepconf_path
#   L6060  _level_write
#   L6091  cmd_level
#   L6135  cmd_auto_project
#   L6173  _skill_local_mats
#   L6193  cmd_auto_skill
#   L6213  _proj_setting_path
#   L6223  _set_yaml_bool
#   L6237  cmd_auto
#   L6278  cmd_adopt
#   L6391  cmd_migrate_subdir

# ===== _write_hpc_yaml (原 L5908-L5923) =====
def _write_hpc_yaml(path, d, note):
    """hpc.yaml 写出（tf 不依赖 PyYAML，手写简单结构；dict 值只到一层）。"""
    keys = [k for k in ("name", "ssh_host", "template_map") if k in d]
    keys += [k for k in d if k not in keys]
    lines = ["# %s\n" % note]
    for k in keys:
        v = d[k]
        if isinstance(v, dict):
            lines.append("%s:\n" % k)
            lines += ["  %s: %s\n" % (k2, v2) for k2, v2 in v.items()]
        elif isinstance(v, list):
            lines.append("%s: [%s]\n" % (k, ", ".join(str(x) for x in v)))
        else:
            lines.append("%s: %s\n" % (k, v))
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

# ===== cmd_hpc (原 L5926-L6013) =====
def cmd_hpc(cfg, types, projs, cluster, tt, yes):
    from tfpkg import _load_yaml_file, discover_local, find_asset, pkg_setting_path, resolve_material_local
    """v1.7：把 -p 指定的项目（一个或多个）分配到指定超算；未指定的项目一律不动。
      tf -p X,Y hpc <集群名>             材料级：改写 project_setting/hpc.yaml
                                         （该材料全部技能生效）
      tf -tt elastic -p X,Y hpc <集群名> 技能级：写/改 材料/<技能>/hpc.yaml
                                         （v1.6 私有配置，优先级最高）
    集群主配置 = 包内 setting/<集群名>.yaml（照 jzzn.yaml 建）；其 template_map
    指向的模板文件须能被找到（skill/<技能>/、project_setting/ 或 <技能>/）。"""
    if not cluster:
        print("错误：缺集群名。用法：tf -p 项目[,项目...] [-tt 技能] hpc <集群名>")
        return 1
    if not projs:
        print("错误：hpc 必须用 -p 显式指定项目（逗号分隔多个）；未指定的不动。")
        return 1
    master_path = pkg_setting_path(cluster + ".yaml")
    if not master_path:
        print("错误：没有集群主配置 setting/%s.yaml。照 jzzn.yaml 建一份："
              "name/ssh_host/template_map（指向 submit_%s_vaspstd_*.tpl 等，"
              "模板文件放进 skill/<技能>/ 目录）。可用集群：%s"
              % (cluster, cluster, _list_pkg_clusters()))
        return 1
    master = _load_yaml_file(master_path)
    if not master.get("ssh_host"):
        print("警告：%s 没写 ssh_host——提交不知道该连哪台。" % master_path)
    todo, seen = [], set()
    for t in types:
        root = t.get("local_root")
        if not root:
            continue
        _r, mats = discover_local(root)
        for m in mats:
            # 同一材料可能被多个配置段重复发现（主流程靠 _dedup_segments
            # 去重，这里自查）：hpc.yaml 按 材料+技能 写，一份就够
            key = (t["key"], os.path.realpath(m["lpath"]))
            if key in seen:
                continue
            resolve_material_local(t, root, m)
            if m["name"] in projs or os.path.basename(m["name"]) in projs:
                seen.add(key)
                todo.append((t, root, m))
    if not todo:
        print("错误：没找到 -p 指定的项目（%s）。" % ", ".join(projs))
        return 1
    print("将把 %d 个项目的%s分配到集群 %s（ssh_host=%s）："
          % (len(todo), (" [%s] 技能" % tt) if tt else "（全部技能）",
             master.get("name") or cluster, master.get("ssh_host") or "未写"))
    for t, root, m in todo:
        print("  %-24s → %s" % (m["name"], ("材料/%s/hpc.yaml" % t["key"])
                                 if tt else "project_setting/hpc.yaml"))
    if not yes:
        ans = input("确认执行？ [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    fails = 0
    for t, root, m in todo:
        if tt:
            sub = m.get("_subdir")
            if not sub:
                print("%s: 失败——技能 %s 没开 skill_subdir，写不了技能私有 "
                      "hpc.yaml（去掉 -tt 改材料级，或先开 subdir）"
                      % (m["name"], t["key"]))
                fails += 1
                continue
            tdir = os.path.join(m["lpath"], sub)
            os.makedirs(tdir, exist_ok=True)
        else:
            tdir = (m.get("ps") or {}).get("dir")
            if not tdir:
                print("%s: 失败——缺 project_setting（先 tf init）" % m["name"])
                fails += 1
                continue
        target = os.path.join(tdir, "hpc.yaml")
        new = _load_yaml_file(target) or {}
        new.update(master)   # 主配置字段全量覆盖；旧文件里的额外字段保留
        _write_hpc_yaml(target, new, "超算配置（tf hpc %s 于 %s 生成/更新）"
                        % (cluster, time.strftime("%Y-%m-%d %H:%M:%S")))
        resolve_material_local(t, root, m)   # 重新解析（带上新写的 hpc.yaml）再查模板
        missing = [lg for lg in (master.get("template_map") or {})
                   if not find_asset(cfg, t, m, lg)]
        note = ("；★ 模板缺失：%s——把文件放进 skill/%s/ 或 %s"
                % (", ".join(missing), t["key"], tdir)) if missing else ""
        print("%s[%s]: hpc → %s（%s）%s"
              % (m["name"], t["key"], master.get("name") or cluster,
                 master.get("ssh_host") or "未写", note))
    print("完成。验证：tf -tt %s 状态表 hpc 列应显示 %s。"
          % (tt or "<技能>", master.get("name") or cluster))
    return 1 if fails else 0

# ===== _list_pkg_clusters (原 L6016-L6025) =====
def _list_pkg_clusters():
    from tfpkg import _PKG_DIR, _PKG_ROOT
    out = []
    for d in (os.path.join(_PKG_ROOT, "setting"),
              os.path.join(_PKG_DIR, "setting"),
              os.path.expanduser("~/.config/taskflow/setting")):
        if os.path.isdir(d):
            out += [f[:-5] for f in os.listdir(d)
                    if f.endswith(".yaml") and f != "tf_default.yaml"]
    return ", ".join(sorted(set(out))) or "（无）"

# ===== _level_stepconf_path (原 L6049-L6057) =====
def _level_stepconf_path(lpath, tkey):
    """<材料>/<技能>/project_setting/templates/step.conf（项目共用层）。"""
    if not lpath:
        return None
    for base in (os.path.join(lpath, str(tkey), "project_setting"),
                 os.path.join(lpath, "project_setting")):
        if os.path.isdir(base):
            return os.path.join(base, "templates", "step.conf")
    return None

# ===== _level_write (原 L6060-L6088) =====
def _level_write(path, level):
    from tfpkg import _LEVEL_HEADER
    """把 [params].BANDGAP 改成 level，其余内容与注释原样保留。"""
    note = ("# 计算级别（tf level 维护）：pbe = 只算到 step3（PBE/PBEsol，"
            "跳过整段 HSE）；hse = 继续算到 step4（HSE06）")
    line = "BANDGAP = %s" % level
    if os.path.isfile(path):
        src = open(path, encoding="utf-8-sig").read()
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        src = _LEVEL_HEADER
    out, hit, in_params = [], False, False
    for ln in src.splitlines():
        st = ln.strip()
        if st.startswith("[") and st.endswith("]"):
            in_params = (st.lower() == "[params]")
        if in_params and not hit and re.match(r"^\s*BANDGAP\s*=", ln):
            out.append(note)
            out.append(line)
            hit = True
            continue
        out.append(ln)
    if not hit:
        if not any(x.strip().lower() == "[params]" for x in out):
            out.append("")
            out.append("[params]")
        idx = max(i for i, x in enumerate(out)
                  if x.strip().lower() == "[params]")
        out[idx + 1:idx + 1] = [note, line]
    open(path, "w", encoding="utf-8").write("\n".join(out).rstrip() + "\n")

# ===== cmd_level (原 L6091-L6132) =====
def cmd_level(cfg, types, tt, proj, arg):
    from tfpkg import _LEVEL_ALIAS, _LEVEL_DESC, _stepconf_param_from_file
    """tf [-tt 技能] [-p 材料] level [pbe|hse] —— 设/查计算级别。"""
    keys = skill_keys(cfg, tt)
    wants = ([x.strip() for x in str(proj).split(",") if x.strip()]
             if proj else None)          # 不给 -p = 该技能下全部材料
    level = None
    if arg is not None:
        level = _LEVEL_ALIAS.get(str(arg).strip().lower())
        if level is None:
            print("错误：level 只接受 pbe / hse（也认 step3 / step4；收到 %r）。"
                  % arg)
            return 1
    fails = 0
    for k in keys:
        names = wants if wants is not None else _skill_local_mats(cfg, types, k)
        if not names:
            print("技能 %s：没发现材料。" % k)
            fails += 1
            continue
        print("技能 %s%s" % (k, ("  ->  %s（%s）" % (level, _LEVEL_DESC[level]))
                            if level else "  当前级别："))
        for w in names:
            lp = resolve_mat_dir(cfg, types, k, w)
            scp = _level_stepconf_path(lp, k)
            if not scp:
                print("  %-28s 还没 init（先 tf -tt %s -p %s init）" % (w, k, w))
                fails += 1
                continue
            cur = _stepconf_param_from_file(scp, "BANDGAP")
            eff = _LEVEL_ALIAS.get(str(cur or "").lower())
            if level is None:
                print("  %-28s %-4s %s"
                      % (w, eff or "hse",
                         "（step.conf 未写，用 skill 出厂默认）" if eff is None
                         else "（%s）" % scp))
                continue
            _level_write(scp, level)
            print("  %-28s %s -> %s" % (w, eff or "(未写)", level))
    if level:
        print("下次 tf / tf start 装配步骤图时生效。已跑完的 step4 产物不会被"
              "删除，只是不再出现在状态表里。")
    return fails

# ===== cmd_auto_project (原 L6135-L6170) =====
def cmd_auto_project(cfg, types, proj, tt, arg):
    from tfpkg import _load_yaml_file
    """v1.9.9：tf [-tt X] -p 材料 auto on|off —— 改该技能项目的
    project_setting/setting.yaml，不动全局 tf.yaml。"""
    keys = skill_keys(cfg, tt)
    wants = [x.strip() for x in str(proj).split(",") if x.strip()]
    if arg is None:
        for w in wants:
            for k in keys:
                lp = resolve_mat_dir(cfg, types, k, w)
                f = _proj_setting_path(lp, k) if lp else None
                cur = (_load_yaml_file(f).get("auto_advance")
                       if f and os.path.isfile(f) else None)
                print("  %-14s %-9s %s" % (w, k, "（无配置）" if not f or
                      not os.path.isfile(f) else
                      ("开" if cur is True else "关")))   # autonow：缺这行 = 关
        return 0
    a = str(arg).strip().lower()
    if a not in ("on", "off", "1", "0", "true", "false", "开", "关"):
        print("错误：auto 只接受 on/off（收到 %r）。" % arg)
        return 1
    on = a in ("on", "1", "true", "开")
    fails = 0
    for w in wants:
        for k in keys:
            lp = resolve_mat_dir(cfg, types, k, w)
            f = _proj_setting_path(lp, k) if lp else None
            if not f or not os.path.isfile(f):
                print("  %s[%s]：还没有 project_setting，先 tf -tt %s -p %s init"
                      % (w, k, k, w))
                fails += 1
                continue
            _set_yaml_bool(f, "auto_advance", on)
            print("  %s[%s]：auto_advance = %s" % (w, k, "true" if on else "false"))
    if not cfg.get("auto_advance"):
        print("注意：全局 auto_advance 还是关的，本开关要配合 tf auto on 才生效。")
    return fails

# ===== _skill_local_mats (原 L6173-L6190) =====
def _skill_local_mats(cfg, types, tt):
    from tfpkg import discover_local
    """patch_auto：列出该技能下本地已发现的材料名（纯本地，不连超算）。"""
    names, seen = [], set()
    for t0 in (types or []):
        if tt and t0.get("key") != tt:
            continue
        lr = t0.get("local_root")
        if not lr:
            continue
        try:
            _r, mats = discover_local(lr)
        except Exception:   # noqa: BLE001
            continue
        for mm in mats:
            if mm["name"] not in seen:
                seen.add(mm["name"])
                names.append(mm["name"])
    return names

# ===== cmd_auto_skill (原 L6193-L6210) =====
def cmd_auto_skill(cfg, types, tt, arg):
    """patch_auto：tf -tt <技能> auto [on|off] —— 对该技能下全部材料批量
    开关项目级 auto_advance；on 时顺手把全局 tf.yaml 也打开。"""
    names = _skill_local_mats(cfg, types, tt)
    if not names:
        print("没有在技能 %s 下发现任何材料（检查 project_roots / local_root）。"
              % tt)
        return 1
    if arg is None:
        print("全局 auto_advance：%s"
              % ("开" if cfg.get("auto_advance") else "关"))
        return cmd_auto_project(cfg, types, ",".join(names), tt, None)
    if str(arg).strip().lower() in ("on", "1", "true", "开"):
        if not cfg.get("auto_advance"):
            cmd_auto(cfg, "on")          # 先开全局，避免下面误报"全局还是关的"
            cfg["auto_advance"] = True
    print("技能 %s：共 %d 个材料 → %s" % (tt, len(names), ", ".join(names)))
    return cmd_auto_project(cfg, types, ",".join(names), tt, arg)

# ===== _proj_setting_path (原 L6213-L6220) =====
def _proj_setting_path(lpath, tkey):
    """材料目录下该技能的 setting.yaml 路径（技能子目录优先，回落材料级）。"""
    if not lpath:
        return None
    a = os.path.join(lpath, str(tkey), "project_setting", "setting.yaml")
    if os.path.isfile(a):
        return a
    return os.path.join(lpath, "project_setting", "setting.yaml")

# ===== _set_yaml_bool (原 L6223-L6234) =====
def _set_yaml_bool(path, key, on):
    """就地改（或追加）一个顶层布尔行，其余内容原样保留。"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    for i, ln in enumerate(lines):
        if re.match(r"^%s\s*:" % re.escape(key), ln):
            lines[i] = "%s: %s\n" % (key, "true" if on else "false")
            break
    else:
        lines.append("%s: %s\n" % (key, "true" if on else "false"))
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

# ===== cmd_auto (原 L6237-L6275) =====
def cmd_auto(cfg, arg):
    """v1.5 tf auto [on|off]：一键开关自动提交（改写全局 tf.yaml 的
    auto_advance 行；没有该行则补在文件头）。无参数 = 显示当前状态。
    只影响 auto_advance；后台监控（auto_watch）不受影响。"""
    path = cfg.get("_config_path")
    if not path:
        print("错误：没有找到配置文件。")
        return 1
    if arg is None:
        print("auto_advance 当前：%s（%s）"
              % ("开" if cfg.get("auto_advance") else "关", path))
        print("切换：tf auto on / tf auto off")
        return 0
    a = str(arg).strip().lower()
    if a not in ("on", "off", "1", "0", "true", "false", "开", "关"):
        print("错误：auto 只接受 on/off（收到 '%s'）。" % arg)
        return 1
    on = a in ("on", "1", "true", "开")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for i, ln in enumerate(lines):
            if re.match(r"^auto_advance\s*:", ln):
                lines[i] = "auto_advance: %s\n" % ("true" if on else "false")
                break
        else:
            lines.insert(0, "auto_advance: %s\n" % ("true" if on else "false"))
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as e:
        print("错误：读写配置失败：%s" % e)
        return 1
    print("auto_advance 已%s（%s）。" % ("开启" if on else "关闭", path))
    if on:
        print("status/watch 会自动提交可开始的步骤；手动 start/retry/rerun 不受影响。")
    else:
        print("status/watch 只看不提交；手动 start/retry/rerun 不受影响。")
        print("后台监控仍在跑（只拉结果）；停监控用 tf watch --stop。")
    return 0

# ===== cmd_adopt (原 L6278-L6388) =====
def cmd_adopt(cfg, types, proj, yes, dry, tt):
    from tfpkg import collect_data, get_types, load_config, merge_project_configs
    """v1.5：接管手工整理的技能子目录结构。适用场景：人手工把 POSCAR、
    project_setting、result、log 搬进了 材料/<技能>/。tf 的规矩是 POSCAR 和
    project_setting 必须在材料根（所有技能共用），<技能>/ 里只放该技能产物。
      第 1 步（本地修正）：POSCAR、project_setting 挪回材料根；
                          根上残留的 result/log 挪进 <技能>/；
      第 2 步（并入迁移）：重新载入配置+采集，逐材料 migrate-subdir——远端
                          step* 移进 <技能>/、项目配置开 skill_subdir；
                          有作业在跑的跳过，算完再跑一次 adopt 即可。
    用法：tf -tt band adopt [--dry-run] [-y] [-p MAT]"""
    if not tt:
        print("错误：adopt 需要 -tt 指定接管哪个技能（如 tf -tt band adopt）。")
        return 1
    raw_tt = ((cfg.get("task_types") or {}).get(tt) or {})
    sub = str(raw_tt.get("dir_name") or tt)
    roots = [os.path.expanduser(r) for r in (cfg.get("project_roots") or [])]
    if not roots:
        print("错误：全局 tf.yaml 没有 project_roots。")
        return 1
    # ---- 第 1 步：扫 <root>/**/<sub>，上一级即材料目录 ----
    targets, plans = [], {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for depth in ("*", "*/*", "*/*/*"):
            for sd in sorted(glob.glob(os.path.join(root, depth, sub))):
                if not os.path.isdir(sd):
                    continue
                D = os.path.dirname(sd)
                if proj and os.path.relpath(D, root) != proj \
                        and os.path.basename(D) != proj:
                    continue
                if (D, root) in targets:
                    continue
                targets.append((D, root))
                fixes = []
                if (not os.path.isfile(os.path.join(D, "POSCAR"))
                        and os.path.isfile(os.path.join(sd, "POSCAR"))):
                    fixes.append((os.path.join(sd, "POSCAR"),
                                  os.path.join(D, "POSCAR"),
                                  "POSCAR 挪回材料根（多技能共用，必须在根）"))
                psd = os.path.join(sd, "project_setting")
                if (os.path.isdir(psd)
                        and not os.path.isdir(os.path.join(D, "project_setting"))):
                    fixes.append((psd, os.path.join(D, "project_setting"),
                                  "project_setting 挪回材料根（band/elastic 段都在里面）"))
                elif os.path.isdir(psd):
                    print("警告：%s 材料根和 %s/ 下各有一份 project_setting，"
                          "adopt 不动——请人工合并后删掉 %s 下那份（保留材料根的），"
                          "否则 %s/ 会被识别成名叫 %s 的新材料。"
                          % (D, sub, sd, sub, sub))
                for d in ("result", "log"):
                    rd = os.path.join(D, d)
                    if os.path.isdir(rd):
                        fixes.append((rd, os.path.join(sd, d),
                                      "%s/ 挪进 %s/" % (d, sub)))
                if fixes:
                    plans[D] = fixes
    if not targets:
        print("没有找到含 %s/ 子目录的材料目录（%s）。" % (sub, ", ".join(roots)))
        return 0
    if plans:
        print("第 1 步：本地布局修正（%d 个材料）：" % len(plans))
        for D, fixes in plans.items():
            for src, dst, why in fixes:
                print("  %-14s %s → %s" % (os.path.basename(D), src, why))
    else:
        print("第 1 步：本地布局无需修正。")
    if dry:
        print("（--dry-run，未执行）")
        return 0
    if plans and not yes:
        ans = input("执行以上移动？ [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    for D, fixes in plans.items():
        for src, dst, _why in fixes:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        print("%s: 布局已修正" % os.path.basename(D))
    # ---- 第 2 步：重新载入配置（第 1 步可能挪回了 project_setting）→ 采集 → 迁移 ----
    print("第 2 步：远端 step* 进 %s/ + 项目配置开 skill_subdir：" % sub)
    cfg2, _ = load_config(cfg.get("_config_path"))
    for k in ("host", "user"):
        if cfg.get(k) is not None:
            cfg2[k] = cfg[k]
    cfg2["_config_dir"] = cfg["_config_dir"]
    cfg2["_config_path"] = cfg["_config_path"]
    cfg2 = merge_project_configs(cfg2)
    types2 = get_types(cfg2, tt=tt)
    if not types2:
        print("错误：%s 还没有任何项目配置段——先 tf init，再重跑 adopt。" % tt)
        return 1
    data2 = collect_data(cfg2, types2)
    by_name = {m["name"]: m for t2 in data2["types"] for m in t2["materials"]}
    fails = 0
    for D, root in targets:
        rel = os.path.relpath(D, root)
        m = by_name.get(rel)
        if m is None:
            print("%s: 跳过——未被识别为材料（材料根缺 POSCAR？补好后 tf init）" % rel)
            continue
        if not (m.get("ps") or {}).get("dir"):
            print("%s: 跳过——缺 project_setting（先 tf -tt %s -p %s init，"
                  "再重跑 adopt）" % (rel, tt, rel))
            continue
        fails += cmd_migrate_subdir(cfg2, data2, rel, True, False)
    print("adopt 完成。用 tf -tt %s 核对：算好的应显示 done；被跳过的"
          "（在跑/缺配置）处理完再跑一次 tf -tt %s adopt -y。" % (tt, tt))
    return 1 if fails else 0

# ===== cmd_migrate_subdir (原 L6391-L6483) =====
def cmd_migrate_subdir(cfg, data, proj, yes, dry):
    from tfpkg import _mat_all_done, log_action, run_remote
    """v1.2：把该技能已完成材料的数据迁进技能子目录（跟着项目走的目录结构）。
    远端 work/材料/step* → work/材料/<技能>/step*；本地 result、log → 材料/<技能>/；
    项目配置该技能段加 skill_subdir: true（状态随即按新路径采集，保持 done）。
    批量模式只迁全部完成的材料；有作业在跑的一律跳过；-p 指定放宽为无作业即可迁。"""
    t = data["types"][0]
    key, sub = t["key"], str(t.get("dir_name") or t["key"])
    mats = [m for m in t["materials"]
            if not proj or m["name"] == proj
            or os.path.basename(m["name"]) == proj]
    if proj and not mats:
        print("错误：%s 下没有材料 %s。" % (key, proj))
        return 1
    todo, skipped = [], []
    for m in mats:
        if m.get("_subdir"):
            skipped.append((m, "已是 %s/ 子目录结构" % sub))
            continue
        jobs = [s for s in m["steps"] if s.get("job")]
        if jobs:   # v1.4.1：jobs 装的是步骤不是作业——取 jobs[0]["job"]["id"]，
                   # 此前 jobs[0]["id"] 直接 KeyError（有在跑作业时崩溃）
            skipped.append((m, "有作业在跑（jobid=%s），算完再迁"
                            % jobs[0]["job"]["id"]))
            continue
        if proj is None and not _mat_all_done(m):
            skipped.append((m, "未全部完成（要迁单个：-p %s migrate-subdir）"
                            % m["name"]))
            continue
        todo.append(m)
    for m, why in skipped:
        print("%s: 跳过——%s" % (m["name"], why))
    if not todo:
        print("没有可迁移的材料。")
        return 0
    print("将把 %d 个材料的 %s 数据迁进 %s/ 子目录：" % (len(todo), key, sub))
    for m in todo:
        print("  %-14s 远端 %s/step* → %s/%s/step*；本地 result、log → %s/"
              % (m["name"], m["path"], m["path"], sub,
                 os.path.join(m["lpath"], sub)))
    if dry:
        print("（--dry-run，未执行）")
        return 0
    if not yes:
        ans = input("确认迁移？ [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return 1
    fails = 0
    for m in todo:
        host = m.get("host_eff") or "__default__"
        tag = "%s[%s]" % (m["name"], key)
        rc, out = run_remote(
            cfg, "cd %s && mkdir -p %s && for d in step*/; do "
                 "[ -d \"$d\" ] && mv \"$d\" %s/; done; true"
                 % (shlex.quote(m["path"]), shlex.quote(sub), shlex.quote(sub)),
            host=host)
        if rc != 0:
            print("%s: 远端迁移失败。%s" % (tag, out))
            fails += 1
            continue
        lp = m["lpath"]
        sdir = os.path.join(lp, sub)
        os.makedirs(sdir, exist_ok=True)
        moved = []
        for d in ("result", "log"):
            srcd = os.path.join(lp, d)
            if os.path.isdir(srcd):
                shutil.move(srcd, os.path.join(sdir, d))
                moved.append(d)
        m["log_dir"] = os.path.join(sdir, "log")   # log 已随迁，改指新位置
        # 项目配置该技能段开 skill_subdir——仅材料级配置才改；
        # 体系级共享配置（多材料共用）改了会误伤未迁的兄弟材料，提示手工处理
        ps = (m.get("ps") or {}).get("dir")
        own_ps = (ps and os.path.isdir(ps) and os.path.dirname(
            os.path.realpath(ps)) == os.path.realpath(lp))
        cfg_note = ""
        if own_ps:
            f0s = glob.glob(os.path.join(ps, "tf_*.yaml"))
            if f0s and _yaml_type_block_ensure(f0s[0], key,
                                               "    skill_subdir: true"):
                cfg_note = "，配置已开 skill_subdir"
            else:
                cfg_note = "，配置已有 skill_subdir"
        else:
            cfg_note = ("，注意：%s，请手工给 %s 段加 skill_subdir: true"
                        % ("项目配置为多材料共享" if ps else
                           "未找到材料级 project_setting", key))
        print("%s: 已迁移（远端 step* + 本地 %s%s）"
              % (tag, "/".join(moved) if moved else "无本地文件", cfg_note))
        log_action(m, "migrate-subdir → %s/（远端 step* + 本地 %s）"
                   % (sub, "/".join(moved)))
    print("迁移完成。tf 查看状态应仍为 done；挂 elastic：tf -tt elastic init")
    return 1 if fails else 0

# ===== 来自 16_watch.py =====
# -*- coding: utf-8 -*-
# 16_watch —— 后台监控 watch（daemon/cron）
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L6846  _watch_files
#   L6854  _watch_running_pid
#   L6870  _watch_pid_alive
#   L6878  _watch_daemon
#   L6911  _watch_stop
#   L6927  _watch_ensure
#   L6953  _watch_cron
#   L6982  _watch_cfg_sig
#   L7013  cmd_watch

# ===== _watch_files (原 L6846-L6851) =====
def _watch_files(cfg=None):
    from tfpkg import WATCH_LOG, WATCH_PID
    """watch 的 pid/log 路径：v1.10 起锚定配置文件所在目录（setting/），
    在任何目录执行 tf watch --stop 都找得到；旧版 cwd 下的 pid 文件由
    _watch_stop 兜底识别。"""
    base = (cfg or {}).get("_config_dir") or os.getcwd()
    return (os.path.join(base, WATCH_PID), os.path.join(base, WATCH_LOG))

# ===== _watch_running_pid (原 L6854-L6867) =====
def _watch_running_pid(cfg=None):
    from tfpkg import WATCH_PID
    """返回在跑的后台监控 PID（没有在跑返回 None）。先看锚定位置，再看 cwd。"""
    cands = [_watch_files(cfg)[0], os.path.abspath(WATCH_PID)]
    for pidfile in dict.fromkeys(cands):
        if not os.path.isfile(pidfile):
            continue
        try:
            pid = int(open(pidfile).read().strip())
        except ValueError:
            pid = None
        if pid and _watch_pid_alive(pid):
            return pid, pidfile
        os.remove(pidfile)
    return None, None

# ===== _watch_pid_alive (原 L6870-L6875) =====
def _watch_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

# ===== _watch_daemon (原 L6878-L6908) =====
def _watch_daemon(a, mat_toks, root, cfg=None):
    """tf watch -d：把监控作为 detached 子进程放后台，日志写配置目录下。"""
    pidfile, logfile = _watch_files(cfg)
    pid, _ = _watch_running_pid(cfg)
    if pid:
        print("tf watch 已在后台运行 (PID %d)" % pid)
        print("日志：%s（tail -f 查看）；停止：tf watch --stop" % logfile)
        return
    argv = [sys.executable, os.path.realpath(sys.argv[0])]
    if a.config:
        argv += ["-c", a.config]
    if a.tt:
        argv += ["-tt", a.tt]
    if a.proj:
        argv += ["-p", a.proj]
    if a.exclude:
        argv += ["-x", a.exclude]
    if a.user:
        argv += ["-u", a.user]
    argv += ["watch", "-i", str(a.interval)]
    if root:
        argv.append(root)
    argv += mat_toks
    log = open(logfile, "ab", buffering=0)
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    with open(pidfile, "w") as f:
        f.write(str(p.pid))
    print("tf watch 已转入后台 (PID %d)" % p.pid)
    print("日志：%s（tail -f %s 查看）" % (logfile, logfile))
    print("停止：tf watch --stop")

# ===== _watch_stop (原 L6911-L6924) =====
def _watch_stop(cfg=None):
    """tf watch --stop：按 pid 文件停止后台监控（任意目录可执行）。"""
    import signal as _sig
    pid, pidfile = _watch_running_pid(cfg)
    if pid:
        os.kill(pid, _sig.SIGTERM)
        try:
            os.remove(pidfile)
        except OSError:
            pass
        print("已停止 tf watch (PID %d)。" % pid)
        return 0
    print("没有运行中的 tf watch。")
    return 1

# ===== _watch_ensure (原 L6927-L6950) =====
def _watch_ensure(cfg):
    """v1.10 auto_watch：任何 tf 命令顺带确保后台监控在跑（没在跑就拉起）。
    配合 tf watch --install 的 crontab 保活 = 零输入全自动：
    重启/WSL 关闭后 cron 拉起；平时敲任何 tf 命令也会顺带拉起。
    保活失败绝不影响主命令。"""
    if not cfg.get("auto_watch"):
        return
    try:
        pid, _ = _watch_running_pid(cfg)
        if pid:
            return
        pidfile, logfile = _watch_files(cfg)
        argv = [sys.executable, os.path.realpath(sys.argv[0])]
        if cfg.get("_config_path"):
            argv += ["-c", cfg["_config_path"]]
        argv += ["watch", "-i", str(cfg.get("watch_interval") or 300)]
        log = open(logfile, "ab", buffering=0)
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        with open(pidfile, "w") as f:
            f.write(str(p.pid))
        print("已自动启动后台监控 tf watch (PID %d)。日志：%s" % (p.pid, logfile))
    except Exception:
        pass

# ===== _watch_cron (原 L6953-L6979) =====
def _watch_cron(install):
    """tf watch --install/--uninstall：crontab 保活——每 10 分钟检查，
    监控死了（重启/崩溃）自动拉起。tf watch -d 有 pid 检查，不会重复启动。"""
    import shutil as _sh
    marker = "# tf-watch-keepalive"
    if not _sh.which("crontab"):
        print("系统没有 crontab 命令。手动保活方案：")
        print("  每 10 分钟执行一次：%s watch -d" % os.path.realpath(sys.argv[0]))
        return 1
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    old = [] if cur.returncode else [l for l in cur.stdout.splitlines()
                                     if marker not in l]
    if install:
        exe = os.path.realpath(sys.argv[0])
        old.append("*/10 * * * * %s watch -d >/dev/null 2>&1  %s"
                   % (exe, marker))
    r = subprocess.run(["crontab", "-"], input="\n".join(old) + "\n",
                       text=True, capture_output=True)
    if r.returncode:
        print("写入 crontab 失败：%s" % (r.stderr or "").strip())
        return 1
    if install:
        print("已写入 crontab 保活：每 10 分钟确保 tf watch 在跑（重启后自动恢复）")
        print("查看：crontab -l；移除：tf watch --uninstall")
    else:
        print("已移除 crontab 保活。")
    return 0

# ===== _watch_cfg_sig (原 L6982-L7010) =====
def _watch_cfg_sig(cfg):
    """v1.8：配置签名——主配置 + project_roots 下全部 *.yaml/*.yml 的
    (路径, mtime_ns, size)（result/log/隐藏目录不扫，里面没有配置）。
    watch 每轮对比，变了自动重载配置。"""
    sig = []
    cp = cfg.get("_config_path")
    if cp and os.path.isfile(cp):
        try:
            st = os.stat(cp)
            sig.append((os.path.realpath(cp), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    for r in (cfg.get("project_roots") or []):
        r = os.path.realpath(os.path.expanduser(r))
        if not os.path.isdir(r):
            continue
        for dp, dn, fn in os.walk(r):
            dn[:] = [d for d in dn
                     if d not in ("result", "log") and not d.startswith(".")]
            for f in fn:
                if f.endswith((".yaml", ".yml")):
                    fp = os.path.join(dp, f)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    sig.append((fp, st.st_mtime_ns, st.st_size))
    sig.sort()
    return tuple(sig)

# ===== cmd_watch (原 L7013-L7075) =====
def cmd_watch(cfg, types, projs, exclude, interval, tt=None, root=None,
              overrides=None):
    from tfpkg import _snapshot, _state_cache_save, apply_exclude, apply_hide_done, apply_skills, auto_advance, auto_fetch, cmd_status, collect_data, fill_local_dim, filter_projs, get_types, load_config, merge_project_configs
    """v3.15 监控模式：每 interval 秒重新采集 → auto-fetch → auto-advance。
    v1.8：每轮检测配置文件改动（tf.yaml / project_setting/*.yaml / 各级
    hpc.yaml），变了自动重载——改配置或换 tf 版本后不用手动重启监控。
    状态有变化才打印总表，否则打印一行心跳。Ctrl+C 退出。"""
    import time as _time
    try:
        sys.stdout.reconfigure(line_buffering=True)  # 重定向/管道时也逐行输出
    except Exception:
        pass

    def _banner(c):
        return ("auto-advance 开" if c.get("auto_advance") else
                "auto-advance 关（tf.yaml 里 auto_advance: true 可开）")
    print("监控模式：每 %d 秒刷新（auto-fetch + %s；配置改动自动重载），"
          "Ctrl+C 退出。" % (interval, _banner(cfg)))
    last = None
    sig = _watch_cfg_sig(cfg)
    try:
        while True:
            s2 = _watch_cfg_sig(cfg)
            if s2 != sig:   # v1.8：配置变了 → 重载（沿用 adopt 的重载路径）
                try:
                    c2, _ = load_config(cfg.get("_config_path"))
                    c2["_config_dir"] = cfg["_config_dir"]
                    c2["_config_path"] = cfg["_config_path"]
                    for k, v in (overrides or {}).items():
                        c2[k] = v          # 命令行 --host/-u 覆盖照旧生效
                    c2 = apply_skills(c2)   # 重载需重装技能骨架（否则 get_types 缺 steps）
                    c2 = merge_project_configs(c2)
                    t2 = get_types(c2, tt=tt, root_override=root)
                    cfg, types = c2, t2
                    sig = _watch_cfg_sig(cfg)   # 以重载后的新配置为准重算
                    last = None                 # 强制重印一次总表
                    print("[%s] 检测到配置变更，已自动重载（%s）。"
                          % (_time.strftime("%H:%M:%S"), _banner(cfg)))
                except (SystemExit, Exception) as e:
                    # 新配置有误：保留旧配置继续跑，文件再改动会再试
                    print("[%s] 配置变更但重载失败（%s），沿用旧配置继续监控。"
                          % (_time.strftime("%H:%M:%S"), e))
                    sig = s2
            data = collect_data(cfg, types)
            fill_local_dim(cfg, data, types)
            # patch_state_cache：把本轮采集结果写进本地缓存，前台 tf list/summary
            # 在 TTL 内直接读它，不用再 ssh 采集一遍。
            _state_cache_save(cfg, data, types, tt, root)
            apply_exclude(data, exclude)
            filter_projs(data, projs)
            auto_fetch(cfg, data)
            auto_recover_hung(cfg, data)   # v1.11: 挂死作业自动恢复（scancel+CONTCAR 续跑）
            auto_advance(cfg, data)
            if cfg.get("hide_done"):
                apply_hide_done(data)   # v1.1：监控里同样支持隐藏完成项
            snap = _snapshot(data)
            if snap != last:
                cmd_status(cfg, data, projs[0] if len(projs) == 1 else None, None)
                last = snap
            else:
                print("[%s] 无变化" % _time.strftime("%H:%M:%S"))
            _time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已退出监控。")

