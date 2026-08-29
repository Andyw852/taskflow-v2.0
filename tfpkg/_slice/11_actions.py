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

