# -*- coding: utf-8 -*-
# 10_summary —— status / summary 命令
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L3614  find_uninited
#   L3638  _mat_all_done
#   L3642  apply_hide_done
#   L3657  cmd_status
#   L3665  _step_status_word
#   L3683  _summary_snapshot
#   L3695  _snapshot_diff
#   L3711  _summary_lines
#   L3754  cmd_summary

# ===== find_uninited (原 L3614-L3635) =====
def find_uninited(cfg):
    """project_roots 下有 POSCAR 但向上找不到 project_setting/tf_*.yaml 的材料目录
    （= 新加进来还没 init 的材料）。
    v1.2 之前只扫已有段的 local_root：新建体系（如 Mg2C60 直接放项目根下）
    不在任何段覆盖范围内，永远扫不到；改为直接扫 project_roots。"""
    import glob as _glob
    out, seen = [], set()
    for proot in (cfg.get("project_roots") or []):
        try:
            root, mats = discover_local(proot)
        except Exception:
            continue
        for m in mats:
            lp = os.path.realpath(m["lpath"])
            if lp in seen:
                continue
            seen.add(lp)
            ps = find_ps_dir(lp, root)
            if ps and _glob.glob(os.path.join(ps, "tf_*.yaml")):
                continue
            out.append(m["name"])
    return out

# ===== _mat_all_done (原 L3638-L3639) =====
def _mat_all_done(m):
    return bool(m["steps"]) and all(s["kind"] == "OK" for s in m["steps"])

# ===== apply_hide_done (原 L3642-L3654) =====
def apply_hide_done(data):
    """v1.1 --hide-done / hide_done: true：状态表隐藏全部步骤都完成的项目。"""
    n = 0
    for t in data["types"]:
        keep = []
        for m in t["materials"]:
            if _mat_all_done(m):
                n += 1
            else:
                keep.append(m)
        t["materials"] = keep
    if n:
        print("（已隐藏 %d 个全部完成的项目；加 --show-done 显示）" % n)

# ===== cmd_status (原 L3657-L3662) =====
def cmd_status(cfg, data, mname, jname):
    if mname:
        t, m = find_material(data, mname)
        render_detail(m)
    else:
        render_table(data)

# ===== _step_status_word (原 L3665-L3680) =====
def _step_status_word(s):
    """步骤状态短词（供快照 diff 用）。刻意不含动态诊断——压力值/力值等每轮
    都会波动的数字不该触发"有变化"，否则巡检每轮都被误报刷屏。"""
    kind = s.get("kind")
    j = s.get("job") or {}
    if kind == "PD":
        r = (j.get("info") or "").strip()
        return "PD(%s)" % r if r else "PD"
    if kind == "FAIL":
        return "FAIL"
    if kind == "R":
        return "R"
    if kind == "OTHER":
        return "OTHER(%s)" % (j.get("state") or "?")
    return {"OK": "done", "WAIT": "wait", "TODO": "todo", "PREP": "prep",
            "SCANCEL": "scancel", "IMAG": "imaginary"}.get(kind, str(kind))

# ===== _summary_snapshot (原 L3683-L3692) =====
def _summary_snapshot(data):
    """结构化快照：type → material → step_label → 状态短词。
    供 summary --diff 做步骤级对比——能精确告诉 agent "谁从什么变到什么"。"""
    snap = {}
    for t in data["types"]:
        tm = {}
        for m in t["materials"]:
            tm[m["name"]] = {s["label"]: _step_status_word(s) for s in m["steps"]}
        snap[t["key"]] = tm
    return snap

# ===== _snapshot_diff (原 L3695-L3708) =====
def _snapshot_diff(old, new):
    """返回变更列表 [(type, material, label, 旧词, 新词)]（按名字排序）。"""
    changes = []
    for tkey in sorted(set(old) | set(new)):
        om = old.get(tkey) or {}
        nm = new.get(tkey) or {}
        for mname in sorted(set(om) | set(nm)):
            os_ = om.get(mname) or {}
            ns_ = nm.get(mname) or {}
            for lab in sorted(set(os_) | set(ns_)):
                ow, nw = os_.get(lab), ns_.get(lab)
                if ow != nw:
                    changes.append((tkey, mname, lab, ow or "-", nw or "-"))
    return changes

# ===== _summary_lines (原 L3711-L3751) =====
def _summary_lines(data):
    """把 data 规约成 summary 的文本行（打印与 diff 共用）。"""
    lines = []
    if not data.get("types"):
        lines.append("（没有任何任务类型）")
        return lines
    for t in data["types"]:
        mats = t["materials"]
        n = len(mats)
        if not n:
            lines.append("%s: 无材料" % t["key"])
            continue
        cnt = {"done": 0, "run": 0, "pd": 0, "err": 0, "sc": 0, "wait": 0}
        fails = []   # (材料名, 步骤label, diag)
        for m in mats:
            kinds = [s["kind"] for s in m["steps"]]
            if all(k == "OK" for k in kinds):
                cnt["done"] += 1
            elif any(k == "FAIL" for k in kinds):
                cnt["err"] += 1
                for s in m["steps"]:
                    if s["kind"] == "FAIL":
                        fails.append((m["name"], s["label"], s.get("diag") or ""))
            elif any(k in ("R", "OTHER") for k in kinds):
                cnt["run"] += 1
            elif any(k == "PD" for k in kinds):
                cnt["pd"] += 1
            elif any(k == "SCANCEL" for k in kinds):
                cnt["sc"] += 1
            else:
                cnt["wait"] += 1
        lines.append("%s: %d 材料 done=%d run=%d pd=%d err=%d scancel=%d wait=%d"
                     % (t["key"], n, cnt["done"], cnt["run"], cnt["pd"],
                        cnt["err"], cnt["sc"], cnt["wait"]))
        for name, lab, diag in fails:
            lines.append("  FAIL %s %s %s" % (name, lab, diag))
    q = data.get("queue")
    if q and (q.get("R") or q.get("PD") or q.get("total")):
        lines.append("队列(全部作业): R=%d PD=%d 共 %d"
                     % (q.get("R", 0), q.get("PD", 0), q.get("total", 0)))
    return lines

# ===== cmd_summary (原 L3754-L3787) =====
def cmd_summary(data, diff=False, state_path=None):
    """只读极简汇总（省 token）：每类型一行计数（run/pd 分开）+ FAIL 清单（含
    诊断原因）+ 全局队列。供 agent 巡检用。

    diff=True 时：与 state_path 里的结构化快照对比，无变化则不输出（token≈0）；
    有变化（或首次）才输出汇总 + 「变更:」步骤级清单（谁从什么变到什么），并写回
    快照——agent 无需再跑 tf list / squeue 去猜哪里变了。
    """
    if diff and state_path:
        new_snap = _summary_snapshot(data)
        try:
            prev_snap = json.loads(open(state_path, encoding="utf-8").read())
        except Exception:
            prev_snap = None
        if prev_snap == new_snap:
            return  # 无变化，静默
        changes = _snapshot_diff(prev_snap, new_snap) if isinstance(prev_snap, dict) else []
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(new_snap, f, ensure_ascii=False, sort_keys=True)
        except OSError:
            pass
        lines = _summary_lines(data)
        if changes:
            print("\n".join(lines))
            print("变更:")
            for _tkey, _mname, _lab, _o, _n in changes[:40]:
                print("  %s %s: %s → %s" % (_mname, _lab, _o, _n))
            if len(changes) > 40:
                print("  … 还有 %d 项变更" % (len(changes) - 40))
        else:
            print("\n".join(lines))
        return
    print("\n".join(_summary_lines(data)))


# ===== 结构化错误码（v2.0：诊断文本 → 稳定 code，供 --json 机器判读）=====
_DIAG_PATTERNS = [
    ("relax_summary_missing", "relax_summary.json missing"),
    ("relax_summary_incomplete", "relax_summary.json incomplete"),
    ("force_not_converged", "force not converged"),
    ("outcar_missing", "OUTCAR missing"),
    ("dir_missing", "dir missing"),
    ("not_started", "not started"),
    ("node_fail", "NODE_FAIL"),
    ("gen_error", "gen 失败"),
    ("stepconf_unknown_params", "不认识的键"),
    ("stepconf_missing", "缺少 step.conf"),
    ("imaginary_freq", "虚频"),
]
_RELAX_VERDICTS = ("electronic", "oscillating", "stalled", "thrown", "nsw",
                   "progressing")


def _diag_code(diag):
    """诊断文本 → 稳定结构化错误码；未命中返回 "unknown"，空串返回 "none"。
    让 agent 无需 grep：直接看 --json 的 step.diag_code / fails[].code。"""
    d = (diag or "").strip()
    if not d:
        return "none"
    for code, pat in _DIAG_PATTERNS:
        if pat in d:
            return code
    if d.startswith("未收敛 ["):
        for v in _RELAX_VERDICTS:
            if "[%s]" % v in d:
                return "relax_" + v
    if d.startswith("job "):
        return "job"
    return "unknown"


def _add_diag_codes(data):
    """给每个步骤 dict 就地补 diag_code 字段（--json 用）。返回 data。"""
    for t in data.get("types", []):
        for m in t.get("materials", []):
            for s in m.get("steps", []):
                s["diag_code"] = _diag_code(s.get("diag") or "")
    return data


def _summary_json(data):
    """summary 的结构化版本（-o json 用）。每类型一行计数 + FAIL 清单 + 队列。"""
    out = {"types": []}
    for t in data["types"]:
        mats = t["materials"]
        n = len(mats)
        if not n:
            out["types"].append({"key": t["key"], "materials": 0, "counts": {},
                                 "fails": []})
            continue
        cnt = {"done": 0, "run": 0, "pd": 0, "err": 0, "scancel": 0, "wait": 0}
        fails = []
        for m in mats:
            kinds = [s["kind"] for s in m["steps"]]
            if all(k == "OK" for k in kinds):
                cnt["done"] += 1
            elif any(k == "FAIL" for k in kinds):
                cnt["err"] += 1
                for s in m["steps"]:
                    if s["kind"] == "FAIL":
                        fails.append({"material": m["name"], "step": s["label"],
                                      "diag": s.get("diag") or "",
                                      "code": _diag_code(s.get("diag") or "")})
            elif any(k in ("R", "OTHER") for k in kinds):
                cnt["run"] += 1
            elif any(k == "PD" for k in kinds):
                cnt["pd"] += 1
            elif any(k == "SCANCEL" for k in kinds):
                cnt["scancel"] += 1
            else:
                cnt["wait"] += 1
        out["types"].append({"key": t["key"], "materials": n, "counts": cnt,
                             "fails": fails})
    q = data.get("queue")
    if q:
        out["queue"] = {"R": q.get("R", 0), "PD": q.get("PD", 0),
                        "total": q.get("total", 0)}
    return out

