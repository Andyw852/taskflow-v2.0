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
    """只生成单步输入（不 sbatch），供达 max_jobs 上限时本地预初始化用。
    返回 True=成功或无需 gen（已生成/本地即时步），False=gen 失败。
    max_jobs 只卡「提交超算」，不卡本地生成输入。"""
    sc = step_cfg(t, s["name"], m)
    if sc.get("run") == "gen" or s["has_incar"]:
        return True
    ok, out = remote_gen(cfg, t, m, s["name"], host=s.get("_host"))
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

