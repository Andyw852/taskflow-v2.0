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

