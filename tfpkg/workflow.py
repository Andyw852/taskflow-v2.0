# -*- coding: utf-8 -*-
"""workflow —— 工作流执行引擎（大深模块）。

由 _slice/06_state.py + 09_submit.py + 13_advance.py + 11_actions.py 合并而来：
状态机 step_state/DAG/门控 + 提交 do_submit/remote_gen + 推进 auto_advance/auto_fetch
+ 动作命令 cmd_start/stop/retry/rerun/clean。四片原本互相缠绕（环1），合并成一个
模块后环消除。对外接口（小）：step_state / do_submit / auto_advance / auto_fetch /
remote_gen / cmd_start / cmd_stop / cmd_retry / cmd_rerun / cmd_clean 等。

外部依赖（00/02/03/04/05/07/08/14 片的名字）在函数内用 from tfpkg import ... 延迟解析。
"""
import os
import sys
import re
import json
import time
import shlex
import base64
import glob
import hashlib
import shutil
import subprocess
import threading


# ===== 来自 06_state.py =====

# -*- coding: utf-8 -*-
# 06_state —— 步骤状态机 / DAG / 技能并发门控
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L2160  _scancel_path
#   L2165  _scancel_load
#   L2178  _scancel_save
#   L2192  _scancel_set
#   L2200  _scancel_clear
#   L2213  step_state
#   L2250  _dag_needs
#   L2262  _dag_max_inflight
#   L2283  _skill_max_jobs
#   L2300  _skill_busy_jobs
#   L2313  _SkillGate
#   L2352  _gen_step_input
#   L2371  _pregenerate_ready
#   L2382  _dep_display
#   L2395  _dag_recompute
#   L2420  annotate
#   L2465  check_duplicates

# ===== _scancel_path (原 L2160-L2162) =====
def _scancel_path(m):
    from tfpkg import SCANCEL_MARK
    lp = m.get("lpath")
    return os.path.join(lp, SCANCEL_MARK) if lp else None

# ===== _scancel_load (原 L2165-L2175) =====
def _scancel_load(m):
    """返回 {"<tt>/<step_name>": {"jobid":..., "time":...}}；无文件/损坏 → {}。"""
    p = _scancel_path(m)
    if not p or not os.path.isfile(p):
        return {}
    try:
        with open(p) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

# ===== _scancel_save (原 L2178-L2189) =====
def _scancel_save(m, marks):
    p = _scancel_path(m)
    if not p:
        return
    try:
        if marks:
            with open(p, "w") as f:
                json.dump(marks, f, ensure_ascii=False, indent=1)
        elif os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass

# ===== _scancel_set (原 L2192-L2197) =====
def _scancel_set(m, step_name, jobid=None):
    """tf stop 成功后调用：给该步骤打 scancel 标记（auto 不再自动重跑）。"""
    marks = _scancel_load(m)
    marks["%s/%s" % (m.get("tt"), step_name)] = {
        "jobid": jobid, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    _scancel_save(m, marks)

# ===== _scancel_clear (原 L2200-L2210) =====
def _scancel_clear(m, step_name=None):
    """step_name=None 清整个材料的标记；否则只清本类型该步骤那条。"""
    marks = _scancel_load(m)
    if step_name is None:
        if marks:
            _scancel_save(m, {})
        return
    key = "%s/%s" % (m.get("tt"), step_name)
    if key in marks:
        del marks[key]
        _scancel_save(m, marks)

# ===== step_state (原 L2213-L2239) =====
def step_state(step, blocked):
    j = step.get("job")
    if j:
        st, info = j["state"], (j.get("info") or "")
        if st == "R":
            return ("R@%s" % info, "R")
        if st == "PD":
            reason = info.strip() or "?"
            if reason.startswith("(") and reason.endswith(")"):
                reason = reason[1:-1]
            return ("PD(%s)" % reason, "PD")
        return (st, "OTHER")
    if step["done"]:
        return ("OK", "OK")
    if step.get("scancel"):   # v1.4：tf stop 取消的标记（压过 FAIL/TODO）
        return ("scancel", "SCANCEL")
    if step.get("imaginary"):
        return ("imaginary", "IMAG")   # 算完了但有虚频，不是 error
    if step.get("plot_error"):
        return ("FAIL", "FAIL")
    if blocked:
        return ("----", "WAIT")
    if step["has_outcar"] or step["has_slurm_out"]:
        return ("FAIL", "FAIL")
    if step["has_incar"]:
        return ("TODO", "TODO")
    return ("PREP", "PREP")

# ===== _dag_needs (原 L2250-L2259) =====
def _dag_needs(t, m, s, prev_name):
    from tfpkg import step_cfg
    """步骤 s 的依赖名列表。skill.yaml 没写 needs 就回退成\"上一步\"，
    这样没改造过的技能（band / elastic）行为完全不变。"""
    sc = step_cfg(t, s["name"], m) or {}
    dep = sc.get("needs")
    if dep is None:
        return [prev_name] if prev_name else []
    if isinstance(dep, str):
        dep = [dep]
    return [str(x) for x in dep]

# ===== _dag_max_inflight (原 L2262-L2271) =====
def _dag_max_inflight(cfg, m):
    from tfpkg import _MAX_INFLIGHT_DEFAULT
    st = (m.get("ps") or {}).get("setting") or {}
    for src in (st, cfg):
        v = src.get("max_inflight")
        if v is not None:
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                pass
    return _MAX_INFLIGHT_DEFAULT

# ===== _skill_max_jobs (原 L2283-L2297) =====
def _skill_max_jobs(cfg, t):
    from tfpkg import _MAX_JOBS_DEFAULT
    """返回该技能（任务类型）的并发提交上限；None = 不限。"""
    tc = (cfg.get("task_types") or {}).get(t.get("key")) or {}
    v = tc.get("max_jobs")
    if v is None:
        v = cfg.get("max_jobs")
    if v is None:
        v = _MAX_JOBS_DEFAULT
    if v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None

# ===== _skill_busy_jobs (原 L2300-L2310) =====
def _skill_busy_jobs(t):
    from tfpkg import _BUSY_KINDS
    """该技能当前已提交（在跑/排队）的超算作业数。
    普通步骤 1 步 = 1 个作业；扇出步骤按 fan_jobids 里的子作业数计。"""
    n = 0
    for m in t.get("materials") or []:
        for s in m.get("steps") or []:
            if s.get("kind") not in _BUSY_KINDS:
                continue
            fj = s.get("fan_jobids")
            n += len(fj) if fj else 1
    return n

# ===== _SkillGate (原 L2313-L2349) =====
class _SkillGate(object):
    """按技能统计「已提交作业数」并卡上限；跨材料共享、线程安全。
    auto_advance 串行、tf start 批量并行都走它，保证同一技能不超 max_jobs。
    同 key 的多个段（v2/v3 混合、多项目）在构造时把 busy 累加在一起。"""
    def __init__(self, cfg, data):
        import threading
        self._lk = threading.Lock()
        self._cap = {}
        self._busy = {}
        for t in (data or {}).get("types") or []:
            key = t.get("key")
            self._cap.setdefault(key, _skill_max_jobs(cfg, t))
            self._busy[key] = self._busy.get(key, 0) + _skill_busy_jobs(t)

    def cap(self, key):
        with self._lk:
            return self._cap.get(key)

    def busy(self, key):
        with self._lk:
            return self._busy.get(key, 0)

    def try_acquire(self, key):
        """原子占一个槽：未超上限则 +1 返回 True，否则 False。"""
        with self._lk:
            cap = self._cap.get(key)
            if cap is None:
                return True
            if self._busy.get(key, 0) >= cap:
                return False
            self._busy[key] += 1
            return True

    def release(self, key):
        with self._lk:
            if key in self._busy:
                self._busy[key] = max(0, self._busy[key] - 1)

# ===== _gen_step_input (原 L2352-L2368) =====
def _gen_step_input(cfg, t, m, s):
    from tfpkg import log_action, step_cfg
    """只生成单步输入（不 sbatch），供达 max_jobs 上限时本地预初始化用。
    返回 True=成功或无需 gen（已生成/本地即时步），False=gen 失败。
    max_jobs 只卡「提交超算」，不卡本地生成输入。"""
    sc = step_cfg(t, s["name"], m)
    if sc.get("run") == "gen" or s["has_incar"]:
        return True
    ok, out = remote_gen(cfg, t, m, s["name"], host=s.get("_host"), wd=s.get("_wd"))
    if not ok:
        print("%s[%s|%s]：预生成输入失败：%s"
              % (m["name"], m["tt"], s["label"], (out or "").strip()))
        return False
    s["has_incar"] = True
    print("%s[%s|%s]：输入已生成（本地待命，等有空位自动提交）。"
          % (m["name"], m["tt"], s["label"]))
    log_action(m, "gen %s（达 max_jobs 上限，先备输入待提交）" % s["label"])
    return True

# ===== _pregenerate_ready (原 L2371-L2379) =====
def _pregenerate_ready(cfg, t, m, fired=None):
    """达 max_jobs 上限时：把该材料当前就绪步骤的输入都生成好（不 sbatch），
    任务从 PREP 变 TODO（输入就绪）；等下一轮有空位时直接提交（跳过 gen）。"""
    for s in (m.get("actives") or []):
        if s is None or s["kind"] not in ("TODO", "PREP"):
            continue
        if fired and s["name"] in fired:
            continue
        _gen_step_input(cfg, t, m, s)

# ===== _dep_display (原 L2382-L2392) =====
def _dep_display(m, name):
    """缺失依赖的显示名：优先材料已有步骤的 label，其次被关闭可选组的 label，
    最后 basename。"""
    for s in m.get("steps") or []:
        if s.get("name") == name:
            return s.get("label") or name
    flat = (m.get("_seg") or {}).get("optional_off_flat") or {}
    hit = flat.get(name)
    if hit:
        return hit[1].get("label") or name
    return os.path.basename(str(name))

# ===== _dag_recompute (原 L2395-L2417) =====
def _dag_recompute(t, m):
    """按 needs 重算每个步骤的 blocked / kind，并返回就绪集（可立即启动的）。
    依赖全部 OK 才算就绪；FAIL / SCANCEL 不自动推进，交给 retry。"""
    names = set(x["name"] for x in m["steps"])
    okset = set()
    for s in m["steps"]:
        _lt, _k = step_state(s, False)
        if _k == "OK":
            okset.add(s["name"])
    ready, prev = [], None
    for s in m["steps"]:
        # 被 optional_steps 关掉的步骤不在 m["steps"] 里，依赖到它要忽略（不卡死），
        # 但记进 _missing_deps，让状态表第三行提醒"下游步骤缺这个数据"。
        raw = _dag_needs(t, m, s, prev)
        deps = [d for d in raw if d in names]
        s["_missing_deps"] = [_dep_display(m, d) for d in raw if d not in names]
        blocked = any(d not in okset for d in deps)
        s["label_txt"], s["kind"] = step_state(s, blocked)
        s["_deps"] = deps
        if s["kind"] in ("TODO", "PREP"):
            ready.append(s)
        prev = s["name"]
    return ready

# ===== annotate (原 L2420-L2462) =====
def annotate(data):
    from tfpkg import FAN_JOBIDS
    for t in data["types"]:
        for m in t["materials"]:
            m["tt"] = t["key"]
            m["desc"] = t["desc"]
            m["troot"] = t["root"]
            blocked = False
            m["active"] = None
            m["action"] = "-"
            marks = _scancel_load(m)   # v1.4：tf stop 打的本步骤标记
            marks_dirty = False
            for s in m["steps"]:
                sc = marks.get("%s/%s" % (t["key"], s["name"]))
                if sc and (s.get("job") or s.get("done")):
                    # 自愈：已有新作业在跑/结果已完成 → 标记失效，清掉
                    del marks["%s/%s" % (t["key"], s["name"])]
                    marks_dirty = True
                    sc = None
                if sc:
                    s["scancel"] = sc
                if s.get("fan_jobids"):        # v1.4
                    FAN_JOBIDS[str(s["fan_jobids"][0])] = \
                        [str(x) for x in s["fan_jobids"]]
            # --- patch_ke_dag: 按 needs 定 blocked，不再是"前面有一步没 OK
            #     后面全 WAIT"。没写 needs 的技能回退成上一步，行为不变。 ---
            _ready = _dag_recompute(t, m)
            m["actives"] = _ready
            m["active"] = next((x for x in m["steps"]
                                if x["kind"] not in ("OK", "WAIT")), None)
            blocked = False
            if marks_dirty:
                _scancel_save(m, marks)
            a = m["active"]
            if a:
                if a["kind"] == "IMAG":
                    m["action"] = "imaginary（声子虚频，动力学不稳定；S6 合理不启动，非 error）"
                elif a["kind"] == "FAIL":
                    m["action"] = "retry " + a["label"]
                elif a["kind"] in ("TODO", "PREP"):
                    m["action"] = "start " + a["label"]
                elif a["kind"] == "SCANCEL":
                    m["action"] = "start " + a["label"] + "（曾被 stop）"
    return data

# ===== check_duplicates (原 L2465-L2485) =====
def check_duplicates(data):
    """同一类型内项目名不允许重复（报错退出；跨段/跨条目聚合检查）；
    basename 重复给警告。跨类型同名允许。"""
    errs = []
    by_key = {}
    for t in data["types"]:
        by_key.setdefault(t["key"], []).extend(m["name"] for m in t["materials"])
    for key, names in by_key.items():
        # v-perf：用 Counter 一次统计，避免大体系下 names.count() 的 O(N²)
        from collections import Counter
        cnt = Counter(names)
        dups = sorted(n for n, c in cnt.items() if c > 1)
        if dups:
            errs.append("任务类型 %s 下项目名称重复：%s" % (key, ", ".join(dups)))
        bcnt = Counter(os.path.basename(n) for n in names)
        bdups = sorted(b for b, c in bcnt.items() if c > 1)
        if bdups:
            print("警告：任务类型 %s 里有重复的 basename：%s，-p 时请写完整名。"
                  % (key, ", ".join(bdups)), file=sys.stderr)
    if errs:
        sys.exit("错误：\n" + "\n".join(errs))


# ===== 来自 09_submit.py =====

# -*- coding: utf-8 -*-
# 09_submit —— 远端生成 / sbatch 提交 / scancel 取消
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L3253  _scancel_desc
#   L3269  remote_scancel
#   L3282  remote_gen
#   L3372  remote_sbatch_fanout
#   L3436  remote_sbatch
#   L3462  kill_if_queued
#   L3479  do_run_gen_step
#   L3513  do_submit
#   L3549  _fanout_guard
#   L3581  do_rerun_step
#   L3607  tag_of

# ===== _scancel_desc (原 L3253-L3266) =====
def _scancel_desc(jobids):
    from tfpkg import FAN_JOBIDS
    """fixte⑫：把代表 jobid 展开成真实取消数量，用于回显。

    扇出步骤一个"代表 jobid"背后是几百个子作业（remote_scancel 会用
    FAN_JOBIDS 展开后一起 scancel）。回显若只打代表号，看着像只取消了一个。
    """
    ids = []
    for x in (jobids or []):
        for y in FAN_JOBIDS.get(str(x), [str(x)]):
            if y not in ids:
                ids.append(y)
    if len(ids) > len(list(jobids or [])):
        return "%d 个作业（代表 %s）" % (len(ids), " ".join(str(x) for x in jobids))
    return " ".join(str(x) for x in ids)

# ===== remote_scancel (原 L3269-L3279) =====
def remote_scancel(cfg, jobids, host="__default__"):
    from tfpkg import FAN_JOBIDS, run_remote
    ids = []                                  # v1.4：代表 jobid → 全部 jobid
    for x in (jobids or []):
        for y in FAN_JOBIDS.get(str(x), [str(x)]):
            if y not in ids:
                ids.append(y)
    jobids = ids
    if not jobids:
        return True, ""
    rc, out = run_remote(cfg, "scancel " + " ".join(jobids), host=host)
    return rc == 0, out

# ===== remote_gen (原 L3282-L3366) =====
def remote_gen(cfg, t, m, sname, host=None, wd=None):
    from tfpkg import STEP_CONF, build_step_conf, find_asset, run_remote, sh_b64, step_cfg
    """执行 gen：先建目录、补 POSCAR（v3 本地模式）和 gen_need 依赖文件、gen 脚本，
    再运行。文件来源：find_asset 查找链（project_setting > skill_dir，支持
    template_map 映射，本地 base64 经 ssh 推送，超算无需存放）> gen_dir
    （远端目录，超算上 cp）。材料目录已有的文件不覆盖。"""
    sc = step_cfg(t, sname, m)
    gen = sc.get("gen")
    if not gen:
        return False, "任务类型 %s 的步骤 %s 没有配置 gen" % (t["key"], sname)
    # v1.13：per-step hpc 拆分时步骤远端目录用自己集群的 work_dir（_wd），
    # 不能用材料级 m["path"]（那是材料默认集群的路径，别的集群上用会 mkdir 错目录）。
    if wd:
        _wd0 = os.path.normpath(os.path.expanduser(str(wd)))
        step_dir = (os.path.join(_wd0, m["name"], m["_subdir"])
                    if m.get("_subdir") else os.path.join(_wd0, m["name"]))
    else:
        step_dir = m["path"]
    gen = gen.format(mat=m["name"], matdir=step_dir, root=t["root"],
                     step=sname, tt=t["key"])
    seg = (m.get("_seg") or {})
    gd = seg.get("gen_dir") or t.get("gen_dir")
    host = host or m.get("host_eff") or "__default__"
    # gen 允许带参数，例如 "gen_step1_PBE_opt.py --stage a"：
    # 前面是脚本名（用于查找/推送），后面原样作为命令行参数传下去。
    _m_py = re.match(r"^(\S+\.py)(\s.*)?$", gen)
    is_py = bool(_m_py)
    gen_script = _m_py.group(1) if _m_py else gen
    gen_args = (_m_py.group(2) or "").strip() if _m_py else ""
    if "gen_need" in sc:   # v1.0：步骤声明 gen_need 则完全替代类型级依赖
        need = list(sc.get("gen_need") or [])       # （画图步不需要 dim_common 等）
    else:
        need = (list(seg.get("gen_need") or t.get("gen_need") or [])
                + list(seg.get("aux_files") or t.get("aux_files") or []))
        # v1.4：template_map 的逻辑名（submit_std_*.tpl 等）始终纳入推送清单。
        # gen 脚本在远端按逻辑名找模板；gen_need 漏写时老材料靠远端残留文件
        # 掩盖，新材料（空目录）就报"找不到模板"——这里兜底，不依赖清单完整。
        for lg in (m.get("template_map") or {}):
            if lg not in need:
                need.append(lg)
    line = "mkdir -p %s && cd %s && " % (shlex.quote(step_dir),
                                         shlex.quote(step_dir))
    if is_py:  # gen 脚本以 skill 为唯一样板：总是覆盖推送（本地改了立即生效）
        gsrc = find_asset(cfg, t, m, gen_script, sname)
        if gsrc:
            with open(gsrc, "rb") as fh:
                gb64 = base64.b64encode(fh.read()).decode()
            line += "echo %s | base64 -d > %s ; " % (gb64, shlex.quote(gen_script))
        else:
            need = need + [gen_script]  # 本地找不到 → gen_dir 远端兜底
    lp = m.get("lpath")
    if lp:  # v3：POSCAR 以本地项目目录为准，远端缺则推送
        pos = os.path.join(lp, "POSCAR")
        if os.path.isfile(pos):
            with open(pos, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            line += "[ -f POSCAR ] || echo %s | base64 -d > POSCAR ; " % b64
    for f in need:
        if f == STEP_CONF:      # v1.9：step.conf 不按单文件推，先本地合并分层
            text, _lg = build_step_conf(cfg, t, m, sname)
            if text is None:
                return False, ("找不到任何 %s（skill/templates 与 "
                               "project_setting/templates 都没有）" % STEP_CONF)
            b64 = base64.b64encode(text.encode("utf-8")).decode()
            line += "echo %s | base64 -d > %s ; " % (b64, shlex.quote(f))
            continue
        local_src = find_asset(cfg, t, m, f, sname)
        if local_src:
            with open(local_src, "rb") as fh:
                data = fh.read()
            b64 = base64.b64encode(data).decode()
            # v1.3.1：依赖文件 md5 比对、不同才覆盖——此前"存在即不推"，
            # 本地 skill 库更新（如 dim_common 加函数）后远端旧版残留，
            # 与新 gen 脚本混用直接 ImportError
            lmd5 = hashlib.md5(data).hexdigest()
            line += ("[ -f %s ] && [ \"$(md5sum %s 2>/dev/null | "
                     "cut -d' ' -f1)\" = %s ] || echo %s | base64 -d > %s ; "
                     % (shlex.quote(f), shlex.quote(f), lmd5,
                        b64, shlex.quote(f)))
        elif gd:
            line += ("[ -f %s ] || { [ -f %s ] && cp %s . || "
                     "{ echo 'ERROR: gen_dir 里缺少 %s' >&2; exit 1; }; }; "
                     % (shlex.quote(f), shlex.quote(os.path.join(t["root"], gd, f)),
                        shlex.quote(os.path.join(t["root"], gd, f)), f))
        else:
            return False, ("project_setting/skill_dir 里缺少 %s，"
                           "且未配置 gen_dir 兜底" % f)
    if is_py:
        line += "python %s%s" % (shlex.quote(gen_script),
                                 (" " + gen_args) if gen_args else "")
        rc, out = run_remote(cfg, line, host=host, use_stdin=True)
    else:
        rc, out = run_remote(cfg, line + sh_b64(gen), host=host, use_stdin=True)
    return rc == 0, out

# ===== remote_sbatch_fanout (原 L3372-L3433) =====
def remote_sbatch_fanout(cfg, s, jobname=None):
    from tfpkg import run_remote, sh_b64
    """扇出步骤：步骤目录下每个匹配子目录各自 sbatch 一次。

    s["fan_todo"] 非空时只提交这些子目录（retry 只补没完成的）；
    为空或缺失时提交全部（首次 gen 之后就是这条路）。
    返回 (是否成功, 输出, 逗号分隔的全部 jobid)。
    """
    pat = str(s.get("fanout"))
    only = s.get("fan_todo") or None
    # patch_fanout_cap：扇出是"每个子目录各 sbatch 一次"的无上限循环，而
    # max_inflight 只数步骤、不数子目录 —— kl 的 findiff 三阶位移动辄上千个，
    # auto_advance 会一口气全交出去，占满作业配额把别的技能一起堵死。
    # 这里先远端数一遍，超阈值直接拒绝（retry 补帧的 fan_todo 不受限）。
    _cap = int(os.environ.get("TF_FANOUT_MAX",
                              str(cfg.get("fanout_max", 200))) or 200)
    if _cap > 0 and not only:
        _rc0, _o0 = run_remote(cfg, sh_b64(
            "cd %s 2>/dev/null && ls -d %s 2>/dev/null | wc -l || echo 0"
            % (shlex.quote(s["dir"]), pat)),
            host=s.get("_host") or "__default__")
        try:
            _n = int((_o0 or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            _n = 0
        if _n > _cap:
            return (False,
                    "扇出 %d 个子目录，超过上限 %d，已拒绝提交。\n"
                    "  确认要交：TF_FANOUT_MAX=%d tf -tt <技能> -p <材料> start\n"
                    "  或先减少位移数：tf -tt kl -p <材料> -j 4 "
                    "conf --set params.METHOD=alm\n"
                    "  永久调阈值：全局 tf.yaml 写 fanout_max: <N>"
                    % (_n, _cap, _n + 1), None)
    cands = [s["submit"]] + [c for c in ("submit.sh", "sub.sh", "job.sh",
                                         "run.sh", "sub.slurm")
                             if c != s["submit"]]
    jn = re.sub(r"[^A-Za-z0-9_.-]", "_", str(jobname or ""))
    ln = ["cd %s || exit 1" % shlex.quote(s["dir"]), "rc=0",
          "ONLY=%s" % (shlex.quote(" ".join(only)) if only else "''"),
          "for d in %s; do" % pat,
          '  [ -d "$d" ] || continue',
          '  if [ -n "$ONLY" ]; then',
          '    case " $ONLY " in *" $d "*) ;; *) continue ;; esac',
          '  fi',
          '  ( cd "$d" || exit 1',
          '    f=""',
          '    for c in %s; do [ -f "$c" ] && f="$c" && break; done' % " ".join(cands),
          '    [ -z "$f" ] && f=$(ls *.sub *.slurm 2>/dev/null | head -1)',
          '    if [ -z "$f" ]; then',
          '      echo "ERROR: $d 里找不到提交脚本" >&2; exit 1',
          '    fi']
    if jn:
        ln.append('    sed -i -e "s/^#SBATCH[[:space:]]\\+--job-name=.*/'
                  '#SBATCH --job-name=%s-$d/" -e "s/^#SBATCH[[:space:]]\\+-J'
                  '[[:space:]].*/#SBATCH --job-name=%s-$d/" "$f" '
                  '2>/dev/null || true' % (jn, jn))
    ln += ['    sbatch "$f" ) || rc=1',
           "done",
           "exit $rc"]
    rc, out = run_remote(cfg, sh_b64("\n".join(ln)),
                         host=s.get("_host") or "__default__")
    jids = re.findall(r"Submitted batch job\s+(\d+)", out or "")
    return (rc == 0 and bool(jids)), out, (",".join(jids) if jids else None)

# ===== remote_sbatch (原 L3436-L3459) =====
def remote_sbatch(cfg, s, jobname=None):
    from tfpkg import run_remote
    if s.get("fanout"):                       # v1.4
        return remote_sbatch_fanout(cfg, s, jobname=jobname)
    cands = [s["submit"]] + [c for c in
                             ("submit.sh", "sub.sh", "job.sh", "run.sh", "sub.slurm")
                             if c != s["submit"]]
    loop = ("f=''; for c in %s; do [ -f \"$c\" ] && f=\"$c\" && break; done; "
            % " ".join(cands))
    loop += ("[ -z \"$f\" ] && f=$(ls *.sub *.slurm 2>/dev/null | head -1); "
             "[ -z \"$f\" ] && { echo 'ERROR: 步骤目录里找不到提交脚本' >&2; exit 1; }; ")
    if jobname:
        jn = re.sub(r"[^A-Za-z0-9_.-]", "_", str(jobname))
        loop += ("sed -i -e 's/^#SBATCH[[:space:]]\\+--job-name=.*/#SBATCH --job-name=%s/' "
                 "-e 's/^#SBATCH[[:space:]]\\+-J[[:space:]].*/#SBATCH --job-name=%s/' \"$f\"; "
                 "grep -q '^#SBATCH --job-name=%s' \"$f\" || "
                 "sed -i '0,/^#SBATCH/s//#SBATCH --job-name=%s\\n&/' \"$f\"; " % (jn, jn, jn, jn))
    loop += "sbatch \"$f\""
    rc, out = run_remote(cfg, "cd %s && %s" % (shlex.quote(s["dir"]), loop),
                         host=s.get("_host") or "__default__")
    jid = None
    m = re.search(r"Submitted batch job\s+(\d+)", out or "")
    if m:
        jid = m.group(1)
    return rc == 0 and jid is not None, out, jid

# ===== kill_if_queued (原 L3462-L3476) =====
def kill_if_queued(cfg, s, force, tag):
    j = s.get("job")
    if not j:
        return True
    if not force:
        if str(j.get("state")) in ("CG", "CF"):   # fixte⑫：正在取消中，不是还在算
            print("%s: 上一批作业正在取消中(CG，SLURM 异步收尾)，"
                  "等几秒再 start；急的话加 -f 强制。" % tag)
            return False
        print("%s: 已有作业 %s(%s)，先 stop 或加 -f。" % (tag, j["id"], j["state"]))
        return False
    ok, out = remote_scancel(cfg, [j["id"]], host=s.get("_host") or "__default__")
    print("%s: scancel %s %s" % (tag, _scancel_desc([j["id"]]),
                                 "成功" if ok else ("失败: " + out)))
    return ok

# ===== do_run_gen_step (原 L3479-L3510) =====
def do_run_gen_step(cfg, t, m, s, tag):
    from tfpkg import log_action, run_remote, step_cfg
    """run: gen 的步骤（v3.21 能带画图等）：只在材料目录远端执行 gen 脚本，
    不提交 SLURM；完成后按 done_marker 复判。失败（目录残留无产出）下次
    状态显示 error，retry/rerun 可重来。"""
    if s.get("job") and not kill_if_queued(cfg, s, True, tag):
        return False
    ok, out = remote_gen(cfg, t, m, s["name"], host=s.get("_host"), wd=s.get("_wd"))
    if not ok:
        print("%s: 运行失败。%s" % (tag, out))
        return False
    marker = step_cfg(t, s["name"], m).get("done_marker") or "band_summary.json"
    rc, o = run_remote(cfg, "test -f %s && echo MARKER_OK"
                       % shlex.quote(os.path.join(s["dir"], marker)),
                       host=s.get("_host") or "__default__")
    if rc == 0 and "MARKER_OK" in (o or ""):
        print("%s: 已生成 %s（%s）" % (tag, s["dir"], marker))
        log_action(m, "plot %s（生成 %s）" % (s["label"], s["dir"]))
        _fetch_stamp_clear(m, s["name"])   # v1.11：产物已更新，让 auto-fetch 重拉
        _scancel_clear(m, s["name"])       # v1.4：重跑成功，清 stop 标记
        if m.get("result_dir"):   # patch_auto2：即时步产物立刻拉回，
            s["done"], s["exists"] = True, True   # 不等下一轮 auto_fetch
            try:
                if fetch_material(cfg, m, only_steps={s["name"]}, quiet=True):
                    print("%s: 已拉回 → %s"
                          % (tag, os.path.join(m["result_dir"], s["name"])))
            except Exception as _e:   # noqa: BLE001
                print("警告：拉回 %s 失败：%s" % (s["name"], _e), file=sys.stderr)
        return True
    tail = (out or "").strip().splitlines()
    print("%s: 脚本运行了但没产出 %s%s（状态将显示 error，检查日志后 retry）"
          % (tag, marker, ("：" + tail[-1]) if tail else ""))
    return False

# ===== do_submit (原 L3513-L3546) =====
def do_submit(cfg, t, m, s, force, gen_first, contcar_cp, tag, submit=True):
    from tfpkg import log_action, run_remote, step_cfg
    """返回 True=成功 / False=失败或被拒绝（供退出码统计）。
    submit=False：只生成输入（gen），不 sbatch、不触发本地生成步，交由 tf start。"""
    if step_cfg(t, s["name"], m).get("run") == "gen":  # v3.21：画图等轻量步骤
        if not submit:
            print("%s: 本地生成步（画图/读取），已就绪，待 tf … start 触发。" % tag)
            return True
        return do_run_gen_step(cfg, t, m, s, tag)
    if not kill_if_queued(cfg, s, force, tag):
        return False
    if gen_first or not s["has_incar"]:
        _relay_prev_across_host(cfg, m, s)   # v1.12：跨集群回传前序产物
        ok, out = remote_gen(cfg, t, m, s["name"], host=s.get("_host"), wd=s.get("_wd"))
        if not ok:
            print("%s: gen 失败。%s" % (tag, out))
            return False
        print("%s: gen 完成。%s" % (tag, out.strip().splitlines()[-1] if out.strip() else ""))
    if contcar_cp:
        run_remote(cfg, "cd %s && [ -f CONTCAR ] && cp CONTCAR POSCAR || true"
                   % shlex.quote(s["dir"]))
    if not submit:   # v3.22：只生成不提交，交由 start
        print("%s: 已生成输入，未提交。检查后运行  tf -p %s -j %s start  提交。"
              % (tag, m["name"].split("/")[-1], s["label"]))
        log_action(m, "gen %s（只生成输入，待 start 提交）" % s["label"])
        return True
    jobname = "%s-%s-%s" % (m["name"].split("/")[-1], m["tt"], s["label"])
    ok, out, jid = remote_sbatch(cfg, s, jobname=jobname)
    print("%s: %s" % (tag, ("已提交 %s (jobid=%s)" % (jobname, jid)) if ok
                            else ("提交失败。" + out)))
    if ok:
        log_action(m, "%s jobid=%s" % (tag.split(" ", 1)[0] + " " + s["label"], jid))
        _fetch_stamp_clear(m, s["name"])   # v1.11：重交后结果会更新，清戳记重拉
        _scancel_clear(m, s["name"])       # v1.4：重交成功，清 stop 标记
    return ok

# ===== _fanout_guard (原 L3549-L3578) =====
def _fanout_guard(m, s, yes, action):
    """kls4：rerun / clean 会 rm -rf 整个步骤目录。扇出步骤（kl 的 S4_disp 等）
    目录里往往躺着几百个算完的位移帧，删掉就是几百个机时；而"补缺帧"用 retry
    就够了（gen 幂等，已有 INCAR+POSCAR 的子目录直接跳过，不动任何文件）。
    所以这里额外拦一道：有已完成子目录就必须完整输入步骤名才放行。
    返回 True=继续，False=中止。"""
    ndone = int(s.get("fan_done") or 0)
    if not s.get("fanout") or ndone <= 0:
        return True
    n = len(s.get("subs") or []) or ndone
    p = str(m["name"]).split("/")[-1]
    print("")
    print("  !! %s[%s] 是扇出步骤，目录里有 %d/%d 个【已算完】的子目录。"
          % (m["name"], s["label"], ndone, n))
    print("     %s 会 rm -rf %s —— 这 %d 个结果全部丢失，要重算。"
          % (action, s["dir"], ndone))
    print("     只是想补没算完的帧的话，用 retry（不删任何文件）：")
    print("       tf -tt %s -p %s -j %s retry" % (m.get("tt", "<技能>"), p, s["label"]))
    print("       tf -tt %s -p %s -j %s start -f" % (m.get("tt", "<技能>"), p, s["label"]))
    if yes:
        print("     （已给 -y，跳过确认，继续 %s）" % action)
        return True
    try:
        ans = input("     确认要删？完整输入步骤名 %s 继续（其他任意键取消）：" % s["name"])
    except EOFError:
        ans = ""
    if ans.strip() != s["name"]:
        print("     已取消，%s 未执行。" % action)
        return False
    return True

# ===== do_rerun_step (原 L3581-L3604) =====
def do_rerun_step(cfg, t, m, s, yes, tag):
    from tfpkg import log_action, run_remote
    if s.get("job"):
        if not kill_if_queued(cfg, s, True, tag):
            return False
    if s["exists"]:
        if not _fanout_guard(m, s, yes, "rerun"):   # kls4
            return False
        if not yes:
            ans = input("删除 %s 并重新生成？ [y/N] " % s["dir"]).strip().lower()
            if ans not in ("y", "yes"):
                print("%s: 已跳过。" % tag)
                return False
        rc, out = run_remote(cfg, "rm -rf -- " + shlex.quote(s["dir"]),
                             host=s.get("_host") or "__default__")
        if rc != 0:
            print("%s: 删除失败。%s" % (tag, out))
            return False
        print("%s: 已删除 %s" % (tag, s["dir"]))
        log_action(m, "rerun %s（删除 %s）" % (s["label"], s["dir"]))
    s2 = dict(s)
    s2["has_incar"] = False
    s2["job"] = None
    return do_submit(cfg, t, m, s2, force=False, gen_first=True, contcar_cp=False,
                     tag=tag, submit=False)

# ===== tag_of (原 L3607-L3608) =====
def tag_of(m, s):
    return "%s[%s|%s]" % (m["name"], m["tt"], s["label"])


# ===== 来自 13_advance.py =====

# -*- coding: utf-8 -*-
# 13_advance —— auto_advance / auto_fetch / clean / step init
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L4776  auto_advance
#   L4888  cmd_step_init
#   L4930  cmd_clean
#   L5091  _fetch_stamp_clear
#   L5101  _relay_prev_across_host
#   L5145  fetch_material
#   L5193  auto_fetch
#   L5256  cmd_fetch

# ===== auto_advance (原 L4776-L4885) =====
def auto_advance(cfg, data):
    from tfpkg import _AUTO_CASCADE_MAX, _BUSY_KINDS, _load_yaml_file, step_cfg
    """status 时自动推进可开始的步骤（全局 tf.yaml 写 auto_advance: true 开启；
    项目 setting.yaml 里 auto_advance: false 可单独关闭）。
    只推进 TODO/PREP（输入就绪/未生成）的活跃步骤；error 不自动重试；
    v1.4：SCANCEL（tf stop 打了标记的）同样不推进，须显式 start/retry/rerun。"""
    if not cfg.get("auto_advance"):
        return
    # fixte⑬：磁盘复核 —— watch 是长驻进程，内存里的 cfg 可能是启动时的旧值
    # （热重载失败会被 except 吞掉，进程继续按旧配置提交作业）。这里每轮直接
    # 读一次 tf.yaml：磁盘上关着就立刻返回，绝不提交。
    _cp = cfg.get("_config_path")
    if _cp and os.path.isfile(_cp):
        try:
            if (_load_yaml_file(_cp) or {}).get("auto_advance") is False:
                return
        except Exception:
            pass
    _skipped = []
    _gate = _SkillGate(cfg, data)   # patch_max_jobs：按技能卡并发提交上限
    for t in data["types"]:
        _sfull = False   # 本技能已到 max_jobs 上限 → 本轮不再提交它的任何材料
        for m in t["materials"]:
            st = (m.get("ps") or {}).get("setting") or {}
            # 未为本技能初始化的材料（无 project_setting -> ps.dir 为空）不自动推进：
            # 否则 band/ke 共用数据根时，ke 会把只 init 了 band 的材料也自动跑起来（误提交）。
            if not (m.get("ps") or {}).get("dir"):
                _skipped.append("%s[%s]（未 init 本技能）" % (m["name"], m["tt"]))
                continue
            if st.get("auto_advance") is not True:   # 显式 true 才推进
                _skipped.append("%s[%s]" % (m["name"], m["tt"]))
                continue
            if _sfull:
                # 本技能已达 max_jobs：只预生成输入（不提交），等有空位自动补交。
                # max_jobs 只卡 sbatch，不卡本地生成输入。
                _pregenerate_ready(cfg, t, m)
                continue
            # --- patch_auto: 级联推进 ------------------------------------
            # 原实现每轮每材料只推一步。画图/读取这类 run:gen 的本地即时步
            # 几秒就完成，却要白等一整个 watch 周期才轮到下一步。这里改成：
            # 交了 SLURM 就停（等作业），本地即时步成功就接着往下推。
            # --- patch_ke_dag: 并行推进 -----------------------------------
            # 原实现只推 m["active"] 一条线。改成遍历 needs 算出的就绪集：
            # 互不依赖的分支（ke 的 S2链 / S3->S4 / S5 / S6 / S7->S7.1）同轮全交。
            # run:gen 的本地即时步（画图、deform read）就地标完成后重算就绪集，
            # 可能立刻解锁下游。max_inflight 兜住 SLURM 的每用户作业数上限。
            _done_now = []
            _cap = _dag_max_inflight(cfg, m)
            _fired = set()
            for _ in range(_AUTO_CASCADE_MAX):
                _busy = sum(1 for x in m["steps"] if x["kind"] in _BUSY_KINDS)
                _ready = [s for s in (m.get("actives") or [])
                          if s["kind"] in ("TODO", "PREP")
                          and s["name"] not in _fired]
                if not _ready:
                    break
                _progress = False
                for s in _ready:
                    sc = step_cfg(t, s["name"], m)
                    _is_gen = sc.get("run") == "gen"
                    # patch_max_jobs：技能级并发提交上限（先于材料级 max_inflight）。
                    # 只卡「提交超算」，不卡本地生成输入：达上限时把本材料剩余
                    # 就绪步骤的输入先生成好（变 TODO），等有空位下一轮直接 sbatch。
                    if not _is_gen and not _gate.try_acquire(t["key"]):
                        print("auto-advance：技能 %s 已提交 %d 个作业，达上限 %d；"
                              "其余就绪步骤先本地生成输入（不提交），等有空位自动补交"
                              "（改 task_types.%s.max_jobs 或 TF_MAX_JOBS 调整）。"
                              % (t["key"], _gate.busy(t["key"]),
                                 _gate.cap(t["key"]), t["key"]))
                        _pregenerate_ready(cfg, t, m, _fired)
                        _sfull = True
                        break
                    if _busy >= _cap:
                        print("auto-advance：%s[%s] 在跑 %d 个已达上限 %d，"
                              "本轮不再提交（改 max_inflight 或 "
                              "TF_MAX_INFLIGHT 调整）"
                              % (m["name"], m["tt"], _busy, _cap))
                        break
                    _fired.add(s["name"])
                    _ok = do_submit(cfg, t, m, s, False, True,
                                    sc.get("contcar_to_poscar"),
                                    "auto %s[%s|%s]"
                                    % (m["name"], m["tt"], s["label"]))
                    if not _ok:
                        if not _is_gen:   # 提交失败：把占的槽还回去
                            _gate.release(t["key"])
                        continue
                    _progress = True
                    if _is_gen:
                        # 本地即时步：就地标完成，下一轮重算就绪集
                        s["done"], s["exists"] = True, True
                        s["kind"], s["label_txt"] = "OK", "OK"
                        if "diag" in s:
                            s["diag"] = "completed"
                        _done_now.append(s["name"])
                    else:
                        _busy += 1
                if _sfull:
                    break
                if not _progress:
                    break
                m["actives"] = _dag_recompute(t, m)
            # patch_auto2：产物已在 do_run_gen_step 里当场拉回，这里不再重复
            # --- patch_auto end ------------------------------------------


    if _skipped:
        print("auto-advance 跳过 %d 个（项目级 auto_advance: false）：%s"
              "  → tf -tt <技能> -p <材料> auto on"
              % (len(_skipped), ", ".join(_skipped[:6])
                 + ("…" if len(_skipped) > 6 else "")))

# ===== cmd_step_init (原 L4888-L4927) =====
def cmd_step_init(cfg, data, proj, job, force):
    from tfpkg import find_material, find_step, log_action, run_remote, step_cfg
    """tf -p MAT -j STEP init：只生成该步骤的输入文件（gen），不提交。
    已有输入时不覆盖（要推倒重来用 rerun）；前序未完成需 -f。"""
    if not proj or not job:
        print("错误：步骤级 init 需要 -p 材料 和 -j 步骤。")
        return 1
    t, m = find_material(data, proj)
    s = find_step(m, job)
    tag = "init %s[%s|%s]" % (m["name"], m["tt"], s["label"])
    if s["kind"] == "OK" and not force:
        print("%s: 该步骤已完成，无需操作（要强制重新生成输入加 -f）。" % tag)
        return 0
    if s.get("job"):
        if not force:
            print("%s: 已有作业 %s(%s)。要杀掉重新生成请加 -f。"
                  % (tag, s["job"]["id"], s["job"]["state"]))
            return 1
        if not kill_if_queued(cfg, s, True, tag):   # v1.9：-f 自动 scancel
            return 1
        s = dict(s)
        s["job"] = None
    if not guard_predecessors(m, s, force):
        return 1
    if s["has_incar"] and not force:
        print("%s: 输入文件已存在（%s）。检查后直接 tf -p %s -j %s start 提交；"
              "要覆盖重新生成加 -f，要连目录一起删掉重来用 rerun。"
              % (tag, s["dir"], proj, job))
        return 0
    sc = step_cfg(t, s["name"], m)
    ok, out = remote_gen(cfg, t, m, s["name"], host=s.get("_host"), wd=s.get("_wd"))
    if not ok:
        print("%s: gen 失败。%s" % (tag, out))
        return 1
    print("%s: gen 完成。%s" % (tag, out.strip().splitlines()[-1] if out.strip() else ""))
    if sc.get("contcar_to_poscar"):
        run_remote(cfg, "cd %s && [ -f CONTCAR ] && cp CONTCAR POSCAR || true"
                   % shlex.quote(s["dir"]), host=s.get("_host") or "__default__")
    log_action(m, "init %s（只生成输入）" % s["label"])
    print("%s: 输入就绪 → %s（检查后用 start 提交）" % (tag, s["dir"]))
    return 0

# ===== cmd_clean (原 L4930-L5085) =====
def cmd_clean(cfg, data, proj, job, yes, purge_config=False):
    from tfpkg import _load_yaml_file, _parallel_map, _yaml_type_block_remove, find_material, find_step, log_action, run_remote
    """删除生成物，回到 PREP（材料级保留 POSCAR）。
    无 -p      → 全部材料；-p 材料 → 该材料；-p 体系目录（如 C20）→ 其下所有材料；
    -p -j      → 单个步骤目录；-j 不带 -p → 全部材料的该步骤目录。
    关联作业一并 scancel。默认要确认，-y 跳过。"""
    if job and not proj:  # v3.11：跨材料只 clean 指定步骤
        tgts = [(m, s) for _, m, s in step_targets(data, job)
                if s.get("exists") or s.get("job")]
        if not tgts:
            print("该步骤在所有材料下都无需清理。")
            return 0
        njob = sum(1 for _, s in tgts if s.get("job"))
        for _fm, _fs in tgts:                        # kls4
            if not _fanout_guard(_fm, _fs, yes, "clean"):
                return 1
        if not yes:
            ans = input("clean 全部材料的步骤 %s（%d 个目录%s）？ [y/N] "
                        % (tgts[0][1]["label"], len(tgts),
                           "，并取消 %d 个作业" % njob if njob else "")
                        ).strip().lower()
            if ans not in ("y", "yes"):
                print("已取消操作。")
                return 1
        fails = 0
        for m, s in tgts:
            host = s.get("_host") or "__default__"
            if s.get("job"):
                remote_scancel(cfg, [s["job"]["id"]], host=host)
                print("%s: scancel %s" % (m["name"], s["job"]["id"]))
            if s.get("exists"):
                rc, out = run_remote(cfg, "rm -rf -- " + shlex.quote(s["dir"]),
                                     host=host)
                if rc != 0:
                    print("%s[%s]: 删除失败。%s" % (m["name"], s["label"], out))
                    fails += 1
                    continue
                print("%s[%s]: 已删除 %s" % (m["name"], s["label"], s["dir"]))
                log_action(m, "clean %s（删除 %s）" % (s["label"], s["dir"]))
            _scancel_clear(m, s["name"])   # v1.4：目录已清，stop 标记一并失效
        return fails
    if proj and job:  # 步骤级
        t, m = find_material(data, proj)
        s = find_step(m, job)
        if not s.get("exists") and not s.get("job"):
            print("%s[%s]: 目录不存在且无作业，无需清理。" % (m["name"], s["label"]))
            return 0
        if not _fanout_guard(m, s, yes, "clean"):    # kls4
            return 1
        if not yes:
            ans = input("clean %s[%s]（删除步骤目录%s）？ [y/N] "
                        % (m["name"], s["label"],
                           "，并取消作业 " + s["job"]["id"] if s.get("job") else "")
                        ).strip().lower()
            if ans not in ("y", "yes"):
                print("已取消操作。")
                return 1
        if s.get("job"):
            remote_scancel(cfg, [s["job"]["id"]],
                           host=s.get("_host") or "__default__")
            print("%s: scancel %s" % (m["name"], s["job"]["id"]))
        if s.get("exists"):
            rc, out = run_remote(cfg, "rm -rf -- " + shlex.quote(s["dir"]),
                                 host=s.get("_host") or "__default__")
            if rc != 0:
                print("%s[%s]: 删除失败。%s" % (m["name"], s["label"], out))
                return 1
            print("%s[%s]: 已删除 %s" % (m["name"], s["label"], s["dir"]))
            log_action(m, "clean %s（删除 %s）" % (s["label"], s["dir"]))
        _scancel_clear(m, s["name"])   # v1.4
        return 0

    if proj:  # 体系目录优先（-p C20 → 其下所有材料），否则按材料解析
        mats = [(t["key"], m) for t in data["types"] for m in t["materials"]
                if "/" in m["name"] and m["name"].split("/")[0] == proj]
        if not mats:
            t, m = find_material(data, proj)
            mats = [(t["key"], m)]
    else:
        mats = [(t["key"], m) for t in data["types"] for m in t["materials"]]
    todo = [(k, m, [s["job"] for s in m["steps"] if s.get("job")])
            for k, m in mats]
    njob = sum(len(j) for _, _, j in todo)
    for _fk, _fm, _fj in todo:                       # kls4：逐个列出会被毁的扇出步骤
        for _fs in _fm.get("steps") or []:
            if not _fanout_guard(_fm, _fs, yes, "clean"):
                return 1
    if not yes:
        ans = input("clean %d 个材料（删除全部生成物，只留 POSCAR%s%s）？ [y/N] "
                    % (len(todo),
                       "，并取消 %d 个作业" % njob if njob else "",
                       "；project_setting 也一并删除，重算需 tf init"
                       if purge_config else
                       "；project_setting 保留，可直接重算")
                    ).strip().lower()
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    def _clean_one(item):
        key, m, jobs = item
        host = m.get("host_eff") or "__default__"
        if jobs:
            remote_scancel(cfg, [j["id"] for j in jobs], host=host)
            print("%s: scancel %s" % (m["name"], " ".join(j["id"] for j in jobs)))
        if not m.get("path"):
            return 0
        line = ("[ -d %s ] && find %s -mindepth 1 -maxdepth 1 ! -name POSCAR "
                "-exec rm -rf -- {} + || true"
                % (shlex.quote(m["path"]), shlex.quote(m["path"])))
        rc, out = run_remote(cfg, line, host=host)
        if rc != 0:
            print("%s: 清理失败。%s" % (m["name"], out))
            return 1
        for d in (m.get("log_dir"), m.get("result_dir")):  # 本地 log/result 一并删
            if d and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        _scancel_clear(m)   # v1.4：材料回到全新状态，stop 标记一并清
        # v1.6：材料自己的 project_setting 一并删除（材料回到全新未初始化状态，
        # 重算需 tf init）；体系级共享配置（如 C20/project_setting）保留不动，
        # 否则会误删兄弟材料的配置。
        # v1.3.4：技能段感知——多技能项目的配置里还有其他技能的段时，只移除
        # 本技能段、保留 project_setting；本技能是最后一个段才整目录删。
        # 此前 tf -tt elastic clean 把整目录删掉，band 段跟着丢、band 瘫痪。
        ps = (m.get("ps") or {}).get("dir")
        lp = m.get("lpath")
        _psr = os.path.realpath(ps) if ps else None
        # v1.7：材料自己的 project_setting 既可能在材料级（dirname == 材料目录），
        # 也可能在技能子目录级（dirname(dirname) == 材料目录，如 Mg2C60/band/project_setting）。
        own_ps = bool(ps and lp and os.path.isdir(ps) and (
            os.path.dirname(_psr) == os.path.realpath(lp) or
            os.path.dirname(os.path.dirname(_psr)) == os.path.realpath(lp)))
        kept_note = ""
        if own_ps and not purge_config:
            # v1.9.7：默认保留 project_setting。它是手写的配置，删了不能自动恢复，
            # 而且删光之后连材料都发现不到（local_root 就写在这些 tf_*.yaml 里），
            # 只能靠在项目根裸跑 tf init 兜回来。要连配置一起清用 --purge-config。
            kept_note = "，project_setting 保留（要连配置一起删加 --purge-config）"
        elif own_ps:
            f0s = glob.glob(os.path.join(ps, "tf_*.yaml"))
            remaining, removed = [], False
            if f0s:
                ex = (_load_yaml_file(f0s[0]).get("task_types") or {})
                remaining = [k for k in ex if k != key]
                if key in ex:
                    removed = _yaml_type_block_remove(f0s[0], key)
            if remaining and removed:
                kept_note = "，已移除 %s 段、project_setting 保留（%s）" % (
                    key, ", ".join(remaining))
            else:
                shutil.rmtree(ps, ignore_errors=True)   # log 已删，不写日志
                kept_note = "，project_setting 已删，重算需 tf init"
        else:
            kept_note = "，体系级共享配置保留"
        print("%s: 已清理（本地+超算只留 POSCAR%s）" % (m["name"], kept_note))
        return 0
    results = _parallel_map(_clean_one, todo, desc="clean")
    return sum(r or 0 for r in results)

# ===== _fetch_stamp_clear (原 L5091-L5098) =====
def _fetch_stamp_clear(m, step_name):
    from tfpkg import FETCH_STAMP
    """步骤重提交/重生成后调用：清掉抓取戳记，让 auto-fetch 重拉新结果。"""
    try:
        sp = os.path.join(m.get("result_dir") or "", step_name, FETCH_STAMP)
        if os.path.isfile(sp):
            os.remove(sp)
    except OSError:
        pass

# ===== _relay_prev_across_host (原 L5101-L5142) =====
def _relay_prev_across_host(cfg, m, s):
    from tfpkg import _ssh_cmd, log_action
    """per-step 跨集群数据传递：当前步骤与其前序步骤不在同一超算时，把本地
    result_dir/<prev>/ 已 fetch 的产物上传到当前 host 的 <prev>/ 目录，让 gen
    脚本的 find_prev_dir 就地找到（FORCES_FC3 等大文件走 tar 流，吃内存低）。"""
    steps = m.get("steps") or []
    idx = next((i for i, x in enumerate(steps) if x.get("name") == s.get("name")), -1)
    if idx <= 0:
        return
    cur_host = s.get("_host")
    cur_dir = s.get("dir")
    rdir = m.get("result_dir")
    if not cur_host or not cur_dir or not rdir:
        return
    for p in reversed(steps[:idx]):
        if not (p.get("exists") or p.get("done")):
            continue
        if p.get("_host") == cur_host:
            return   # 同集群：gen 就地能找到，无需回传
        local = os.path.join(rdir, p["name"])
        if not os.path.isdir(local):
            fetch_material(cfg, m, only_steps={p["name"]}, quiet=True)
        if not os.path.isdir(local):
            continue   # 前序产物拉取失败
        remote_prev = os.path.join(os.path.dirname(cur_dir), p["name"])
        remote = "mkdir -p %s && tar -xf - -C %s" % (
            shlex.quote(remote_prev), shlex.quote(remote_prev))
        p1 = subprocess.Popen(["tar", "-cf", "-", "-C", local, "."],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen(_ssh_cmd(cfg, cur_host, [remote]),
                              stdin=p1.stdout, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
        p1.stdout.close()
        rc1 = p1.wait()
        _, err2 = p2.communicate()
        if rc1 != 0 or p2.returncode != 0:
            print("警告：跨集群回传 %s → %s 失败：%s"
                  % (p["name"], remote_prev, (err2 or b"").decode().strip()))
            continue
        print("%s: 已把 %s 产物回传到 %s → %s"
              % (m["name"], p["label"], cur_host, remote_prev))
        log_action(m, "relay %s → %s" % (p["label"], cur_host))
        return

# ===== fetch_material (原 L5145-L5190) =====
def fetch_material(cfg, m, only_steps=None, quiet=False, all_files=False):
    from tfpkg import FETCH_STAMP, _ssh_cmd, log_action
    """把该材料各已存在步骤的 fetch_files 从超算拉回本地 result_dir/<step>/。
    用 tar 管道流式传输，缺失文件自动跳过。only_steps = 只拉这些步骤名。"""
    host = m.get("host_eff") or cfg.get("host")
    files = m.get("fetch_files") or []
    if not files:
        if not quiet:
            print("%s: fetch_files 为空，跳过。" % m["name"])
        return True
    nstep = 0
    for s in m["steps"]:
        if not s.get("exists"):
            continue
        if only_steps is not None and s["name"] not in only_steps:
            continue
        dest = os.path.join(m["result_dir"], s["name"])
        os.makedirs(dest, exist_ok=True)
        sc2 = next((x for x in ((m.get("_seg") or {}).get("steps_cfg") or [])
                    if x.get("name") == s["name"]), {})
        if sc2.get("fetch_all") or all_files:   # v3.21：画图步骤产物文件名不固定，整目录拉回；v1.1：fetch --all 整目录
            remote = "cd %s && tar -cf - ." % shlex.quote(s["dir"])
        else:
            remote = ("cd %s && tar --ignore-failed-read -cf - %s"
                      % (shlex.quote(s["dir"]),
                         " ".join(shlex.quote(f) for f in files)))
        s_host = s.get("_host") or host   # v1.12+：每步在各自集群，按步骤 host 拉
        cmd1 = (_ssh_cmd(cfg, s_host, [remote]) if s_host else ["bash", "-c", remote])
        p1 = subprocess.Popen(cmd1, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
        p2 = subprocess.run(["tar", "xf", "-", "-C", dest], stdin=p1.stdout,
                            capture_output=True, text=True)
        p1.stdout.close()
        rc1 = p1.wait()
        if rc1 != 0 or p2.returncode != 0:
            print("%s: fetch %s 失败。%s" % (m["name"], s["label"], p2.stderr))
            return False
        try:   # v1.11：抓取成功写戳记（auto-fetch 据此跳过，不再每次重拉）
            open(os.path.join(dest, FETCH_STAMP), "w").close()
        except OSError:
            pass
        nstep += 1
    if nstep and not quiet:
        print("%s: 已拉回 %d 个步骤 → %s" % (m["name"], nstep, m["result_dir"]))
    if nstep:
        log_action(m, "fetch %d 个步骤 → %s" % (nstep, m["result_dir"]))
    return True

# ===== auto_fetch (原 L5193-L5253) =====
def auto_fetch(cfg, data):
    from tfpkg import FETCH_STAMP
    """status 时自动把"已完成但尚未拉回"的步骤结果保存到本地（本地模式；
    项目 setting.yaml 里 auto_fetch: false 可关闭）。失败只警告不打断。"""
    pending = []
    for t in data["types"]:
        for m in t["materials"]:
            if not m.get("result_dir"):
                continue
            st = (m.get("ps") or {}).get("setting") or {}
            if st.get("auto_fetch") is False:
                continue
            need = []
            for s in m["steps"]:
                if not s.get("done"):
                    continue
                dest = os.path.join(m["result_dir"], s["name"])
                # v1.11：按戳记判"已抓取"。旧守卫（dest 非空）对跳过段失效——
                # 它们远端没目录，永远拉不到文件，会每次都重试空拉
                if os.path.isfile(os.path.join(dest, FETCH_STAMP)):
                    continue
                # v1.12：老版本已拉回过的（dest 非空）补写戳记直接跳过——否则
                # 戳记制上线第一次会把全部历史结果（含巨大的 HSE OUTCAR、整目录
                # 画图产物）重拉一遍，表现为长时间"卡死"
                if os.path.isdir(dest) and os.listdir(dest):
                    try:
                        open(os.path.join(dest, FETCH_STAMP), "w").close()
                    except OSError:
                        pass
                    continue
                need.append(s["name"])
            if not need:
                continue
            pending.append((m, need))
    if not pending:
        return
    for m, need in pending:
        print("auto-fetch %s: %s → %s" % (m["name"], ",".join(need),
                                          m["result_dir"]))

    def one(mn):
        m, need = mn
        try:
            ok = fetch_material(cfg, m, only_steps=set(need), quiet=True)
        except Exception as e:  # noqa: BLE001
            ok = False
            print("警告：auto-fetch %s 失败：%s" % (m["name"], e),
                  file=sys.stderr)
        if ok:  # 戳记补写：跳过段远端没目录，fetch 不会经手，这里统一补上
            for nm in need:
                try:
                    d = os.path.join(m["result_dir"], nm)
                    os.makedirs(d, exist_ok=True)
                    open(os.path.join(d, FETCH_STAMP), "w").close()
                except OSError:
                    pass
    if len(pending) == 1:
        one(pending[0])
    else:  # v3.17：多材料并行拉回（配合 ssh 连接复用，等待时间大幅缩短）
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as ex:
            list(ex.map(one, pending))

# ===== cmd_fetch (原 L5256-L5269) =====
def cmd_fetch(cfg, data, mname, all_files=False):
    from tfpkg import find_material
    fails = 0
    if mname:
        t, m = find_material(data, mname)
        mats = [m]
    else:
        mats = [m for t in data["types"] for m in t["materials"]]
    for m in mats:
        if not m.get("result_dir"):
            print("%s: 非本地模式（无 result_dir），跳过 fetch。" % m["name"])
            continue
        if not fetch_material(cfg, m, all_files=all_files):
            fails += 1
    return fails


# ===== 来自 11_actions.py =====

# -*- coding: utf-8 -*-
# 11_actions —— start / stop / retry / rerun 命令
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L3790  _start_ready
#   L3864  _retry_targets
#   L3869  guard_predecessors
#   L3894  cmd_start
#   L3976  cmd_stop
#   L4068  retry_submit
#   L4082  cmd_retry
#   L4129  find_step_soft
#   L4143  step_targets
#   L4156  _optional_off_hit
#   L4178  _def_matches
#   L4191  _fresh_step_dict
#   L4205  enable_optional_group
#   L4258  cmd_rerun
#   L4271  _cmd_rerun
#   L4324  rerun_project

# ===== _start_ready (原 L3790-L3861) =====
def _start_ready(cfg, t, m, force, incl_scancel=False, gate=None):
    from tfpkg import _BUSY_KINDS, step_cfg
    """patch_start_dag：把一个材料当前所有就绪步骤交掉，返回失败数。

    就绪 = _dag_recompute 算出的 actives（依赖全 OK 的 TODO/PREP）。
    没打过 DAG 补丁、或技能没写 needs 时 actives 里只会有一个，
    行为退化成原来的"只交 active"。

    gate：patch_max_jobs 的技能级并发提交上限（跨材料共享）；None 则不限。
    """
    # 未为本技能初始化的材料（无 project_setting -> ps.dir 为空）不 start：
    # 与 auto_advance 一致，避免共用数据根时 start 误跑没 init 的材料。
    if not (m.get("ps") or {}).get("dir"):
        print("跳过 %s[%s]：未 init 本技能（先 tf -tt %s -p %s init）。"
              % (m["name"], m["tt"], m["tt"], m["name"]))
        return 0
    fails = 0
    ready = m.get("actives")
    if ready is None:
        s = m.get("active")
        ready = [s] if s is not None else []
    cap = _dag_max_inflight(cfg, m)
    busy = sum(1 for x in m["steps"] if x["kind"] in _BUSY_KINDS)
    fired = 0
    _fired = set()
    for s in ready:
        if s is None or s["kind"] in ("R", "PD", "OTHER", "OK"):
            continue
        if s["kind"] == "FAIL":
            print("NEEDS_RETRY: %s [%s|%s] %s（确认后用 tf -tt %s -p '%s' "
                  "-j %s retry）"
                  % (m["name"], m["tt"], s["label"], s.get("diag", ""),
                     m["tt"], m["name"], s["label"]))
            continue
        if s["kind"] == "SCANCEL" and not incl_scancel:
            print("SCANCEL: %s [%s|%s] 曾被 tf stop 取消，不会自动重跑"
                  "（重跑：tf -tt %s -p '%s' -j %s start，或 "
                  "-status scancel start）"
                  % (m["name"], m["tt"], s["label"], m["tt"], m["name"],
                     s["label"]))
            continue
        sc = step_cfg(t, s["name"], m)
        _is_gen = sc.get("run") == "gen"
        # patch_max_jobs：技能级并发提交上限（先于材料级 max_inflight）。
        # 只卡 sbatch，不卡本地生成输入：达上限时把剩余就绪步骤输入先生成好。
        if gate is not None and not _is_gen and not gate.try_acquire(m["tt"]):
            print("%s[%s]：技能 %s 已提交 %d 个作业，达上限 %d，未提交的任务"
                  "先本地生成输入（不提交），等有空位自动补交（改 "
                  "task_types.%s.max_jobs 或 TF_MAX_JOBS 调整）。"
                  % (m["name"], m["tt"], m["tt"], gate.busy(m["tt"]),
                     gate.cap(m["tt"]), m["tt"]))
            _pregenerate_ready(cfg, t, m, _fired)
            break
        if busy >= cap:
            print("%s[%s]：在跑 %d 个已达上限 %d，剩下的没交"
                  "（改 max_inflight 或 TF_MAX_INFLIGHT）"
                  % (m["name"], m["tt"], busy, cap))
            break
        _fired.add(s["name"])
        ok = do_submit(cfg, t, m, s, force, gen_first=False,
                       contcar_cp=sc.get("contcar_to_poscar", False),
                       tag="start " + tag_of(m, s))
        if ok:
            fired += 1
            busy += 1
        else:
            if gate is not None and not _is_gen:   # 提交失败：还回占的槽
                gate.release(m["tt"])
            fails += 1
    if not fired and not fails:
        print("%s[%s]：没有可启动的步骤（都在跑、已完成，或被依赖卡住）。"
              % (m["name"], m["tt"]))
    return fails

# ===== _retry_targets (原 L3864-L3866) =====
def _retry_targets(m, retryable):
    """patch_start_dag：所有可 retry 的步骤（原来只看 active 一个）。"""
    return [s for s in m["steps"] if retryable(s)]

# ===== guard_predecessors (原 L3869-L3891) =====
def guard_predecessors(m, s, force):
    """-j 指定的步骤若前序未完成，需 -f 确认才继续。返回 True=可继续。

    patch_start_dag：优先看 _dag_recompute 写进来的真实依赖 _deps；
    没有 _deps 的技能（没写 needs 的 band/elastic/kl）回退成
    "清单里排在它前面的都要 OK"，行为与改造前一致。
    """
    if force:
        return True
    deps = s.get("_deps")
    if deps is not None:
        byname = {x["name"]: x for x in m["steps"]}
        bad = [byname[d] for d in deps
               if d in byname and byname[d]["kind"] != "OK"]
    else:
        idx = m["steps"].index(s)
        bad = [x for x in m["steps"][:idx] if x["kind"] != "OK"]
    if bad:
        print("%s: 前序步骤 %s 未完成（%s），gen 依赖上一步产物，不能直接生成；"
              "请先完成前序（或确认输入已手动就绪后加 -f 强制）。"
              % (m["name"], "/".join(x["label"] for x in bad), bad[0]["label_txt"]))
        return False
    return True

# ===== cmd_start (原 L3894-L3973) =====
def cmd_start(cfg, data, mname, jname, force, incl_scancel=False):
    from tfpkg import _parallel_map, find_material, find_step, step_cfg
    """返回失败次数（含被拒绝的操作）。
    incl_scancel：-status scancel 显式筛选过时，SCANCEL 步骤也可被 start
    （否则批量 start 一律跳过被 stop 标记的步骤——v1.4"不会自动重跑"）。"""
    fails = 0
    if jname and not mname:  # v3.11：对全部材料只 start 指定步骤
        gate = _SkillGate(cfg, data)   # patch_max_jobs：技能级并发提交上限
        for t, m, s in step_targets(data, jname):
            if not (m.get("ps") or {}).get("dir"):
                continue   # 未 init 本技能的材料不 start
            if s["kind"] == "OK" or s.get("job"):
                continue
            if s["kind"] == "SCANCEL" and not incl_scancel:
                print("跳过 %s[%s|%s]：scancel 标记（-status scancel start 可重跑）。"
                      % (m["name"], m["tt"], s["label"]))
                continue
            if not guard_predecessors(m, s, force):
                continue
            if s["kind"] == "FAIL" and not force:
                print("%s[%s|%s]: FAIL 状态，建议 retry/rerun（start 需 -f）。"
                      % (m["name"], m["tt"], s["label"]))
                continue
            sc = step_cfg(t, s["name"], m)
            _is_gen = sc.get("run") == "gen"
            if not _is_gen and not gate.try_acquire(m["tt"]):
                print("技能 %s 已提交 %d 个作业，达上限 %d，%s 先本地生成输入"
                      "（不提交），等有空位自动补交（改 task_types.%s.max_jobs "
                      "或 TF_MAX_JOBS 调整）。"
                      % (m["tt"], gate.busy(m["tt"]), gate.cap(m["tt"]),
                         m["name"], m["tt"]))
                _gen_step_input(cfg, t, m, s)
                continue
            if not do_submit(cfg, t, m, s, force, gen_first=False,
                             contcar_cp=sc.get("contcar_to_poscar", False),
                             tag="start " + tag_of(m, s)):
                if not _is_gen:
                    gate.release(m["tt"])
                fails += 1
        return fails
    if mname:
        t, m = find_material(data, mname)
        if jname:
            s = find_step_soft(m, jname)
            if s is None:
                # 步骤不在工作流里：可能是被 optional_steps 关掉的组（如 BANDGAP=pbe
                # 时的 HSE 分支）。允许按需启用并直接生成/提交。
                hit = _optional_off_hit(m, jname)
                if hit is None:
                    find_step(m, jname)   # 触发原报错（列出已有步骤），不返回
                    return 1
                flag, defs = hit
                print("%s[%s]：步骤 %s 未启用（可选组 %s 被关），"
                      "现按需启用该组并生成/提交。"
                      % (m["name"], m["tt"], jname, flag))
                s = enable_optional_group(cfg, t, m, flag, defs, jname)
                if s is None:
                    find_step(m, jname)
                    return 1
            if not guard_predecessors(m, s, force):
                return 1
            if s["kind"] == "FAIL" and not force:
                print("%s: 步骤 %s 处于 FAIL，建议用 retry（保留文件）或 "
                      "rerun（推倒重来）；确定要 start 请加 -f。"
                      % (m["name"], s["label"]))
                return 1
            ok = do_submit(cfg, t, m, s, force, gen_first=False,
                           contcar_cp=step_cfg(t, s["name"], m).get(
                               "contcar_to_poscar", False),
                           tag="start " + tag_of(m, s))
            return 0 if ok else 1
        # --- patch_start_dag：不带 -j 时交掉整个就绪集 -----------------
        return _start_ready(cfg, t, m, force, incl_scancel,
                            gate=_SkillGate(cfg, data))
    items = [(t, m) for t in data["types"] for m in t["materials"]]
    _gate = _SkillGate(cfg, data)   # patch_max_jobs：跨材料共享，线程安全
    results = _parallel_map(
        lambda tm: _start_ready(cfg, tm[0], tm[1], force, incl_scancel,
                                gate=_gate),
        items, desc="start")
    return sum(r or 0 for r in results)

# ===== cmd_stop (原 L3976-L4065) =====
def cmd_stop(cfg, data, mname, jname, yes):
    from tfpkg import find_material, find_step, log_action
    if jname and not mname:  # v3.11：取消全部材料的指定步骤作业
        jobs = [(s["job"], m, s) for _, m, s in step_targets(data, jname)
                if s.get("job")]
        if not jobs:
            print("该步骤没有排队/运行的作业。")
            return 0
        desc = ", ".join("%s(%s,%s)" % (j["id"], m["name"], j["state"])
                         for j, m, s in jobs)
        if not yes:
            ans = input("取消全部材料的步骤 %s 的作业：%s ? [y/N] "
                        % (jobs[0][2]["label"], desc)).strip().lower()
            if ans not in ("y", "yes"):
                print("已取消操作。")
                return 1
        by_host = {}
        for j, m, s in jobs:
            by_host.setdefault(m.get("host_eff") or "__default__",
                               []).append((j, m, s))
        ok_all = True
        for h, trio in by_host.items():
            ids = [j["id"] for j, m, s in trio]
            ok, out = remote_scancel(cfg, ids, host=h)
            ok_all = ok_all and ok
            print("scancel %s %s" % (_scancel_desc(ids),
                                     "成功" if ok else ("失败: " + out)))
            if ok:   # v1.4：打 scancel 标记，auto_advance 不再自动重跑
                for j, m, s in trio:
                    _scancel_set(m, s["name"], j["id"])
                    log_action(m, "stop %s" % j["id"])
        if ok_all:
            print("已打 scancel 标记（不会自动重跑）；重跑："
                  "tf -status scancel start（保留文件）或 rerun（推倒重来）")
        return 0 if ok_all else 1
    if mname:
        t, m = find_material(data, mname)
        steps = [find_step(m, jname)] if jname else m["steps"]
        jobs = [(s["job"], s) for s in steps if s.get("job")]
        if not jobs:
            print("%s: 没有排队/运行的作业。" % m["name"])
            return 0
        desc = ", ".join("%s(%s,%s)" % (j["id"], j["state"], s["label"]) for j, s in jobs)
        if not yes:
            ans = input("取消 %s(tt=%s) 的作业 %s ? [y/N] "
                        % (m["name"], m["tt"], desc)).strip().lower()
            if ans not in ("y", "yes"):
                print("已取消操作。")
                return 1
        ok, out = remote_scancel(cfg, [j["id"] for j, _ in jobs],
                                 host=m.get("host_eff") or "__default__")
        print("%s: scancel %s %s" % (m["name"], _scancel_desc([j["id"] for j, _ in jobs]),
                                     "成功" if ok else ("失败: " + out)))
        if ok:   # v1.4：打 scancel 标记，auto_advance 不再自动重跑
            for j, s in jobs:
                _scancel_set(m, s["name"], j["id"])
            log_action(m, "stop %s" % " ".join(j["id"] for j, _ in jobs))
            print("%s: 已打 scancel 标记（不会自动重跑）；重跑："
                  "tf -tt %s -p '%s' start（保留文件）或 rerun（推倒重来）"
                  % (m["name"], m["tt"], m["name"]))
        return 0 if ok else 1
    jobs = [(s["job"], m, s) for t in data["types"] for m in t["materials"]
            for s in m["steps"] if s.get("job")]
    if not jobs:
        print("没有任何排队/运行的作业。")
        return 0
    desc = ", ".join("%s(%s|%s,%s)" % (j["id"], m["name"], m["tt"], s["label"])
                     for j, m, s in jobs)
    if not yes:
        ans = input("取消全部 %d 个作业：%s ? [y/N] " % (len(jobs), desc)).strip().lower()
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    by_host = {}
    for j, m, s in jobs:
        by_host.setdefault(m.get("host_eff") or "__default__", []).append((j, m, s))
    ok_all = True
    for h, trio in by_host.items():
        ids = [j["id"] for j, m, s in trio]
        ok, out = remote_scancel(cfg, ids, host=h)
        ok_all = ok_all and ok
        print("scancel %s %s" % (_scancel_desc(ids),
                                     "成功" if ok else ("失败: " + out)))
        if ok:   # v1.4：打 scancel 标记，auto_advance 不再自动重跑
            for j, m, s in trio:
                _scancel_set(m, s["name"], j["id"])
                log_action(m, "stop %s" % j["id"])
    if ok_all:
        print("已打 scancel 标记（不会自动重跑）；重跑："
              "tf -status scancel start（保留文件）或 rerun（推倒重来）")
    return 0 if ok_all else 1

# ===== retry_submit (原 L4068-L4079) =====
def retry_submit(cfg, t, m, s, force, tag):
    from tfpkg import step_cfg
    """v1.9：retry = 先 scancel 在跑的作业 -> 用【项目配置】重新生成输入 -> 重新提交。
    与 rerun 的区别：retry 不删除步骤目录（保留 OUTCAR/CONTCAR 等已有产物），
    只覆盖生成的输入文件；rerun 会 rm -rf 整个步骤目录。"""
    if s.get("job") and not kill_if_queued(cfg, s, True, tag):
        return False
    s2 = dict(s)
    s2["job"] = None
    return do_submit(cfg, t, m, s2, force, gen_first=True,
                     contcar_cp=step_cfg(t, s["name"], m).get(
                         "contcar_to_poscar", False),
                     tag=tag, submit=False)

# ===== cmd_retry (原 L4082-L4126) =====
def cmd_retry(cfg, data, mname, jname, force, incl_scancel=False):
    from tfpkg import find_material, find_step
    """incl_scancel：-status scancel 显式筛选过时，SCANCEL 步骤也可 retry
    （v1.4；默认批量 retry 只动 FAIL，不碰 scancel 标记的步骤）。"""
    fails = 0

    def _retryable(s):
        return s["kind"] == "FAIL" or (incl_scancel and s["kind"] == "SCANCEL")

    if jname and not mname:  # v3.11：重交全部材料的指定 FAIL 步骤
        for t, m, s in step_targets(data, jname):
            if not _retryable(s):
                continue
            if not guard_predecessors(m, s, force):
                continue
            if not retry_submit(cfg, t, m, s, force, "retry " + tag_of(m, s)):
                fails += 1
        return fails
    if mname:
        t, m = find_material(data, mname)
        if jname:
            s = find_step(m, jname)
            if not guard_predecessors(m, s, force):
                return 1
            if s is None:
                print("%s: 已全部完成。" % m["name"])
                return 0
            ok = retry_submit(cfg, t, m, s, force, "retry " + tag_of(m, s))
            return 0 if ok else 1
        # patch_start_dag：不带 -j 时重交所有 FAIL 步骤（可能分散在多条分支）
        tgt = _retry_targets(m, _retryable)
        if not tgt:
            print("%s[%s]: 没有需要 retry 的步骤。" % (m["name"], m["tt"]))
            return 0
        for s in tgt:
            if not retry_submit(cfg, t, m, s, force,
                                "retry " + tag_of(m, s)):
                fails += 1
        return fails
    for t in data["types"]:
        for m in t["materials"]:
            for s in _retry_targets(m, _retryable):
                if not retry_submit(cfg, t, m, s, force,
                                    "retry " + tag_of(m, s)):
                    fails += 1
    return fails

# ===== find_step_soft (原 L4129-L4140) =====
def find_step_soft(m, jname):
    from tfpkg import _find_by_dotted, _step_seq_match
    """find_step 的宽容版：材料没有该步骤时返回 None 而不是退出。"""
    steps = m["steps"]
    if jname.isdigit():
        for s in steps:
            if _step_seq_match(s, int(jname)):
                return s
        return None
    for s in steps:
        if s["name"] == jname or s["label"] == jname:
            return s
    return _find_by_dotted(steps, jname)    # v1.8：-j 2.1 点号序号

# ===== step_targets (原 L4143-L4153) =====
def step_targets(data, jname):
    """跨材料定位同一步骤：返回 [(t, m, s)]，无此步骤的材料跳过。"""
    out = []
    for t in data["types"]:
        for m in t["materials"]:
            s = find_step_soft(m, jname)
            if s is not None:
                out.append((t, m, s))
    if not out:
        sys.exit("错误：没有任何材料有步骤 '%s'。" % jname)
    return out

# ===== _optional_off_hit (原 L4156-L4175) =====
def _optional_off_hit(m, jname):
    from tfpkg import _name_seq, _seq_key, step_seq
    """在被关闭的可选组里找 jname（label/name/seq）。返回 (flag, defs) 或 None。"""
    seg = m.get("_seg") or {}
    flat = seg.get("optional_off_flat") or {}
    hit = flat.get(jname)
    if hit:
        flag = hit[0]
        defs = (seg.get("optional_off") or {}).get(flag) or []
        return flag, [dict(x) for x in defs]
    want = _seq_key(jname)
    if want is None:
        return None
    for flag, defs in (seg.get("optional_off") or {}).items():
        for d in defs:
            sq = _seq_key(step_seq(d))
            if sq is None:
                sq = _name_seq(d.get("name"))
            if sq is not None and abs(sq - want) < 1e-9:
                return flag, [dict(x) for x in defs]
    return None

# ===== _def_matches (原 L4178-L4188) =====
def _def_matches(d, jname):
    from tfpkg import _name_seq, _seq_key, step_seq
    """可选组步骤定义是否命中 -j token（label/name/seq）。"""
    if jname == str(d.get("name")) or jname == str(d.get("label")):
        return True
    want = _seq_key(jname)
    if want is None:
        return False
    sq = _seq_key(step_seq(d))
    if sq is None:
        sq = _name_seq(d.get("name"))
    return sq is not None and abs(sq - want) < 1e-9

# ===== _fresh_step_dict (原 L4191-L4202) =====
def _fresh_step_dict(m, sname, sc):
    """按需启用可选组时给新步骤造一个运行时状态（字段与采集器一致）。"""
    d = os.path.normpath(os.path.join(m["path"], sname))
    f = {"name": sname, "label": sc.get("label") or sname, "dir": d,
         "exists": False, "has_incar": False, "has_outcar": False,
         "has_slurm_out": False, "done": False, "diag": "",
         "submit": sc.get("submit", "submit.sh"), "job": None,
         "_host": m.get("host_eff")}
    if sc.get("check") == "plot" or sc.get("run") == "gen":
        f["plot"] = True
        f["diag"] = "not started"
    return f

# ===== enable_optional_group (原 L4205-L4255) =====
def enable_optional_group(cfg, t, m, flag, defs, jname):
    from tfpkg import _seq_sort_steps, _yaml_type_block_set
    """按需启用被关闭的可选组：写入项目配置持久化 + 注入当前材料工作流。
    返回 jname 对应的步骤 dict（找不到返回 None）。"""
    # 1) 持久化：项目配置 task_types.<tt> 写 flag: true。step.conf 的 BANDGAP
    #    等开关优先级低于项目配置显式值（见 resolve_stepconf_flags），下次
    #    tf 装配步骤图时该组自然恢复。
    src = (m.get("_seg") or {}).get("_from")
    if src and os.path.isfile(src):
        _yaml_type_block_set(src, m.get("tt") or t.get("key"), flag, True)
    # 2) 注入完整步骤定义到 per-material steps_cfg（不污染同段其它材料）。
    #    配置 defs 带 seq，追加后统一按 seq 重排即得正确顺序（等价于该组一开始就开着）。
    seg = m.setdefault("_seg", {})
    scfg = list(seg.get("steps_cfg") or t.get("steps_cfg") or t.get("steps") or [])
    cfg_names = {s.get("name") for s in scfg}
    for d in defs:
        if d.get("name") in cfg_names:
            continue
        scfg.append({k: v for k, v in d.items() if k != "after"})
        cfg_names.add(d.get("name"))
    scfg = _seq_sort_steps(scfg)
    seg["steps_cfg"] = scfg
    # 从 optional_off 摘掉该组，避免重复注入
    off = dict(seg.get("optional_off") or {})
    off.pop(flag, None)
    seg["optional_off"] = off
    flat = {}
    for f2, ds2 in off.items():
        for d in ds2:
            for k in ("name", "label", "seq"):
                v = d.get(k)
                if v is not None:
                    flat[str(v)] = (f2, d)
    seg["optional_off_flat"] = flat
    # 3) 注入运行时步骤到 m["steps"]，并按 steps_cfg 顺序重排（运行时步骤没有 seq，
    #    用 name-seq 排会丢掉小数位，故直接对齐配置顺序，通用且稳定）。
    steps = list(m.get("steps") or [])
    existing = {s.get("name") for s in steps}
    cfg_map = {s.get("name"): s for s in scfg}
    for d in defs:
        nm = d.get("name")
        if nm in existing or nm not in cfg_map:
            continue
        steps.append(_fresh_step_dict(m, nm, cfg_map[nm]))
        existing.add(nm)
    order = {s.get("name"): i for i, s in enumerate(scfg)}
    m["steps"] = sorted(steps, key=lambda s: order.get(s.get("name"), 10 ** 6))
    m["actives"] = _dag_recompute(t, m)
    m["active"] = next((x for x in m["steps"]
                        if x["kind"] not in ("OK", "WAIT")), None)
    matched_name = next((d.get("name") for d in defs if _def_matches(d, jname)), None)
    return find_step_soft(m, matched_name) if matched_name else None

# ===== cmd_rerun (原 L4258-L4268) =====
def cmd_rerun(cfg, data, mname, jname, yes, force=False, from_skill=False):
    """rerun = scancel -> rm -rf 步骤目录 -> 重新生成 -> 重新提交。
    from_skill=True（--from-skill）：忽略 project_setting/ 与 材料/<技能>/ 下的
    模板与 step.conf，只用 skill 库的出厂版本生成（hpc.yaml / setting.yaml 等
    项目配置仍然生效，否则连队列和账号都对不上）。"""
    global _SKILL_ONLY
    _SKILL_ONLY = bool(from_skill)
    try:
        return _cmd_rerun(cfg, data, mname, jname, yes, force)
    finally:
        _SKILL_ONLY = False

# ===== _cmd_rerun (原 L4271-L4321) =====
def _cmd_rerun(cfg, data, mname, jname, yes, force=False):
    from tfpkg import _parallel_map, find_material, find_step
    fails = 0
    if jname and not mname:  # v3.11：跨材料只 rerun 指定步骤
        tgts = step_targets(data, jname)
        todo, skip = [], []
        for t, m, s in tgts:
            if s["kind"] == "OK":
                skip.append("%s[%s] 已完成" % (m["name"], s["label"]))
                continue
            if not guard_predecessors(m, s, force):
                skip.append("%s[%s] 前序未完成" % (m["name"], s["label"]))
                continue
            todo.append((t, m, s))
        for msg in skip:
            print("跳过 %s" % msg)
        if not todo:
            print("没有可 rerun 的目标。")
            return 0
        if not yes:
            ans = input("rerun 全部材料的步骤 %s（%d 个目标：删除并重新生成提交）？ [y/N] "
                        % (todo[0][2]["label"], len(todo))).strip().lower()
            if ans not in ("y", "yes"):
                print("已取消操作。")
                return 1
        for t, m, s in todo:
            if not do_rerun_step(cfg, t, m, s, True, tag="rerun " + tag_of(m, s)):
                fails += 1
        return fails
    if not mname:
        mats = [(t, m) for t in data["types"] for m in t["materials"]]
        if not yes:
            ans = input("rerun 全部 %d 个材料（各自清空整个工作目录，保留 POSCAR，"
                        "含本地 result，从头重新生成）？ [y/N] "
                        % len(mats)).strip().lower()
            if ans not in ("y", "yes"):
                print("已取消操作。")
                return 1
        results = _parallel_map(
            lambda tm: rerun_project(cfg, tm[0], tm[1], yes=True),
            mats, desc="rerun")
        return sum(0 if r else 1 for r in results)
    t, m = find_material(data, mname)
    if jname:
        s = find_step(m, jname)
        if not guard_predecessors(m, s, force=False):
            print("（rerun 会删除并重新生成，前序缺失时 gen 必然失败；"
                  "如确已手动备好前序产物，请用 start -f 或先处理前序。）")
            return 1
        ok = do_rerun_step(cfg, t, m, s, yes, tag="rerun " + tag_of(m, s))
        return 0 if ok else 1
    return 0 if rerun_project(cfg, t, m, yes) else 1

# ===== rerun_project (原 L4324-L4366) =====
def rerun_project(cfg, t, m, yes):
    from tfpkg import log_action, run_remote
    """整材料 rerun：清空整个 <材料>/<技能> 远程工作目录（只保留 POSCAR）
    + 删本地 log/result，再从第一步从头生成提交。彻底从零，不留任何旧步骤
    目录或结果。project_setting 在本地、不在工作目录内，故 BANDGAP 等参数与
    模板保留不动（要连配置一起清用 tf clean --purge-config）。
    注意：带 -j 的单步 rerun 走 do_rerun_step，仍只删该步，不受此影响。"""
    jobs = [s["job"] for s in m["steps"] if s.get("job")]
    if not yes:
        ans = input("清空 %s(tt=%s) 的整个 %s 工作目录（保留 POSCAR，含本地 result）"
                    "并从头重新生成？ [y/N] "
                    % (m["name"], m["tt"], m["tt"])).strip().lower()
        if ans not in ("y", "yes"):
            print("%s: 已跳过。" % m["name"])
            return False
    host = m.get("host_eff") or "__default__"
    if jobs:
        remote_scancel(cfg, [j["id"] for j in jobs], host=host)
        print("%s: scancel %s" % (m["name"], " ".join(j["id"] for j in jobs)))
    if m.get("path"):
        # 与 tf clean 同款：清空工作目录内一切、只留 POSCAR（重生成要用它）
        line = ("[ -d %s ] && find %s -mindepth 1 -maxdepth 1 ! -name POSCAR "
                "-exec rm -rf -- {} + || true"
                % (shlex.quote(m["path"]), shlex.quote(m["path"])))
        rc, out = run_remote(cfg, line, host=host)
        if rc != 0:
            print("%s: 清空工作目录失败。%s" % (m["name"], out))
            return False
        print("%s: 已清空工作目录（保留 POSCAR）" % m["name"])
    for d in (m.get("log_dir"), m.get("result_dir")):   # 本地 log/result 一并删
        if d and os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            print("%s: 已删本地 %s" % (m["name"], d))
    log_action(m, "rerun project（清空整个工作目录，保留 POSCAR）")
    _scancel_clear(m)   # v1.4：整材料推倒重来，清掉全部 stop 标记
    if not m["steps"]:
        print("%s: 该类型没有配置步骤。" % m["name"])
        return False
    first = m["steps"][0]
    s0 = dict(first)
    s0["has_incar"] = False
    s0["job"] = None
    return do_submit(cfg, t, m, s0, force=False, gen_first=True, contcar_cp=False,
                     tag="rerun " + tag_of(m, first), submit=False)
