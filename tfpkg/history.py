# -*- coding: utf-8 -*-
"""history —— 步骤状态的时间序列（history.jsonl）+ tf history 命令（v1.0 W5–8）。

为什么要它：现在只有"此刻的状态"（tf list/summary）和"与上次比变了什么"
（tf summary --diff）。于是
  · 一个材料什么时候开始跑、排队排了多久、FAIL 了几次、谁把它救回来的，
    全靠 monitor 日志里翻；
  · 新技能加进来后，"这个技能的历史"根本不存在——没有地方记。
本模块把每次采集到的**状态转移**追加成 JSONL 事件流，于是**任何技能、任何材料
自动就有 history**，不需要每个技能自己写日志代码。

文件（都在 tf 配置目录 / setting/ 下，不进 git）：
  history.jsonl            事件流，每行一个 JSON（追加写，跨会话保留）
  .tf_history_state.json   上次采集的状态快照（用于 diff；不是给人看的）

记录时机：任何**真正采集**（tf list/summary/status/json/monitor）之后自动记录；
首次运行只落基线快照、不写事件（避免给几千个材料刷一屏"首次见到"）。

事件字段：
  ts    ISO 时间（本地时区，秒）
  mat   材料名（如 C24/qHPC24）
  skill 技能 key（如 band-dft-cpu）
  step  步骤名 / label
  ev    state（状态词变了）| diag（状态没变但诊断文本变了）
        | action（我们**做的事**：start/retry/rerun/stop/clean/init/fetch/gen）
  f     变化前状态词（首次见到为 null）
  t     变化后状态词（OK/R/PD/FAIL/TODO/PREP/WAIT/SCANCEL）
  diag  变化后的诊断文本（截断）
  job   作业号（有则记）
  host  集群 ssh 别名（有则记）
"""

import datetime
import json
import os

HIST_NAME = "history.jsonl"
STATE_NAME = ".tf_history_state.json"
_MAX_BYTES = 8 * 1024 * 1024     # 超过就裁到最近 _KEEP_LINES 行
_KEEP_LINES = 40000
_MAX_EVENTS_PER_RUN = 800        # 单轮最多写多少事件（防首次大体系刷爆）


def history_path(cfg):
    """事件流文件路径（配置目录下；没有配置目录就退回 cwd）。"""
    return os.path.join((cfg or {}).get("_config_dir") or os.getcwd(), HIST_NAME)


def history_state_path(cfg):
    return os.path.join((cfg or {}).get("_config_dir") or os.getcwd(), STATE_NAME)


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _secs_between(ts_a, ts_b):
    """两个 "%Y-%m-%dT%H:%M:%S" 之间相差多少秒；解析不了返回 None。"""
    try:
        a = datetime.datetime.strptime(str(ts_a)[:19], "%Y-%m-%dT%H:%M:%S")
        b = datetime.datetime.strptime(str(ts_b)[:19], "%Y-%m-%dT%H:%M:%S")
        return max(0, int((b - a).total_seconds()))
    except ValueError:
        return None


def _fmt_dur(sec):
    if sec is None:
        return ""
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def _trim(path):
    """文件过大时裁到最近 _KEEP_LINES 行（追加写，裁剪是低频操作）。"""
    try:
        if os.path.getsize(path) <= _MAX_BYTES:
            return
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-_KEEP_LINES:])
    except OSError:
        pass


def history_record(cfg, data, force=False):
    """把本轮采集的状态转移追加进 history.jsonl，返回写入的事件数。

    data 是 collect_data 的产物（types[].materials[].steps[]）。
    没有状态文件时（首次运行）只落基线、不写事件——除非 force=True。"""
    path, sp = history_path(cfg), history_state_path(cfg)
    try:
        with open(sp, encoding="utf-8") as f:
            prev = json.load(f)
        if not isinstance(prev, dict):
            prev = None
    except Exception:
        prev = None
    ts = _now()
    cur, events = {}, []
    for t in (data or {}).get("types", []):
        key = t.get("key")
        for m in t.get("materials", []):
            mname = m.get("name")
            host = m.get("host_eff")
            for s in m.get("steps", []):
                sk = "%s\t%s\t%s" % (mname, key, s.get("name"))
                diag = (s.get("diag") or "")[:160]
                job = (s.get("job") or {}).get("id")
                kind = s.get("kind")
                old = prev.get(sk) if isinstance(prev, dict) else None
                # v1.0：记住"第一次看到它在跑/排队"的时刻，结束那一下就能算出
                # 真实墙钟（dur 秒）——作业号 + 墙钟正是论文里要的 per-step 事实。
                started = (old or {}).get("s")
                if kind in ("R", "PD"):
                    started = started or ts
                elif kind in ("TODO", "PREP", "WAIT"):
                    started = None
                cur[sk] = {"k": kind, "d": diag, "j": job, "s": started}
                if prev is None and not force:
                    continue
                if old and old.get("k") == kind and old.get("d") == diag:
                    continue
                if old is None and not force:
                    continue      # 新出现的步骤：先记基线，下一轮起才记事件
                ev = "state" if (old or {}).get("k") != kind else "diag"
                dur = None
                if old is not None and kind in ("OK", "FAIL") \
                        and old.get("k") in ("R", "PD"):
                    ev = "finish" if kind == "OK" else "fail"
                    dur = _secs_between(old.get("s") or ts, ts)
                events.append({
                    "ts": ts, "mat": mname, "skill": key,
                    "step": s.get("label") or s.get("name"),
                    "ev": ev, "f": (old or {}).get("k"), "t": kind,
                    "diag": diag, "job": job, "host": host, "dur": dur,
                })
    dropped = 0
    if len(events) > _MAX_EVENTS_PER_RUN:
        dropped = len(events) - _MAX_EVENTS_PER_RUN
        events = events[:_MAX_EVENTS_PER_RUN]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if events:
            with open(path, "a", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            _trim(path)
        tmp = sp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
        os.replace(tmp, sp)
    except OSError:
        return 0
    if dropped:
        import sys as _sys
        _sys.stderr.write("警告（history）：本轮变更过多，只记了前 %d 条，"
                          "丢弃 %d 条。\n" % (_MAX_EVENTS_PER_RUN, dropped))
    return len(events)


# =============================================================================
# 读取（tf history）
# =============================================================================
def history_action(cfg, mat=None, skill=None, step=None, action=None,
                   host=None, job=None, note=None):
    """记一条**动作**事件（我们做过什么：提交/重生成/停止…）。

    与状态事件同流：这样 `tf history` 一句话就能回答"谁在什么时候把它重交的"，
    不用翻 monitor 日志。只追加、绝不抛（失败返回 False）。"""
    import sys as _sys
    if not action:
        return False
    e = {"ts": _now(), "mat": mat, "skill": skill, "step": step,
         "ev": "action", "act": str(action), "t": str(action),
         "host": host, "job": job, "note": (str(note)[:200] if note else None)}
    path = history_path(cfg)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        _trim(path)
        return True
    except OSError as exc:
        _sys.stderr.write("警告（history）：动作事件没记上：%s\n" % exc)
        return False


def history_load(cfg, proj=None, tt=None, since=None, limit=None):
    """读出事件流并按材料/技能/时间过滤。返回 (事件列表, 总行数)。"""
    path = history_path(cfg)
    out, total = [], 0
    if not os.path.isfile(path):
        return out, 0
    since_ts = _parse_since(since)
    wants = _name_set(proj)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                total += 1
                if tt and e.get("skill") != tt:
                    continue
                if wants and not _match_proj(e.get("mat"), wants):
                    continue
                if since_ts and str(e.get("ts") or "") < since_ts:
                    continue
                out.append(e)
    except OSError:
        return [], 0
    if limit:
        out = out[-int(limit):]
    return out, total


def _name_set(proj):
    if not proj:
        return set()
    return {x.strip() for x in str(proj).split(",") if x.strip()}


def _match_proj(mat, wants):
    if not mat:
        return False
    return mat in wants or os.path.basename(mat) in wants


def _parse_since(since):
    """"7d" / "12h" / "90m" / "2026-09-14" / "2026-09-14 10:00" → 起始时间串。"""
    s = str(since or "").strip()
    if not s:
        return None
    unit = s[-1].lower()
    if unit in ("d", "h", "m") and s[:-1].isdigit():
        n = int(s[:-1])
        delta = {"d": 86400, "h": 3600, "m": 60}[unit] * n
        t0 = datetime.datetime.now() - datetime.timedelta(seconds=delta)
        return t0.strftime("%Y-%m-%dT%H:%M:%S")
    return s.replace(" ", "T")


def cmd_history(cfg, proj=None, tt=None, since=None, last_n=40, json_out=False):
    """tf history [-p 材料] [-tt 技能] [--since 7d] [-n 40] [--json]

    只读：直接读 history.jsonl，不采集、不连超算、不提交。
    记录是**自动**的——任何一次真正采集（tf list/summary/status/monitor）之后
    都会把状态转移追加进去，任何技能加进来就自动有历史。"""
    path = history_path(cfg)
    evs, total = history_load(cfg, proj=proj, tt=tt, since=since)
    if json_out:
        print(json.dumps({"path": path, "total": total, "count": len(evs),
                          "events": evs[-int(last_n or 40):]},
                         ensure_ascii=False, indent=2))
        return 0
    if total == 0:
        print("还没有历史记录（%s 不存在或为空）。" % path)
        print("历史是**自动**记的：跑一次会采集的命令即可开始积累——")
        print("  tf list --refresh      # 或 tf summary --refresh / tf status / tf monitor")
        print("（首次采集只落基线快照，不写事件；下一次采集起，状态变化逐条落 history.jsonl）")
        return 0
    show = evs[-int(last_n or 40):]
    head = "历史记录  %s" % path
    print(head)
    scope = []
    if proj:
        scope.append("材料 %s" % proj)
    if tt:
        scope.append("技能 %s" % tt)
    if since:
        scope.append("时间 ≥ %s" % since)
    print("范围      %s" % ("  ".join(scope) if scope else "全部"))
    print("共 %d 条匹配（文件累计 %d 条），显示最近 %d 条："
          % (len(evs), total, len(show)))
    print("")
    if not evs:
        print("（该范围内没有记录）")
        return 0
    print("%-19s %-18s %-14s %-12s %-16s %s"
          % ("时间", "材料", "技能", "步骤", "变化", "诊断"))
    for e in show:
        f, t = e.get("f") or "-", e.get("t") or "-"
        arrow = "%s → %s" % (f, t)
        detail = e.get("diag") or ""
        if e.get("ev") == "diag":
            arrow = "诊断变化（%s）" % t
        elif e.get("ev") == "action":
            # 动作事件：t 存的是动作名，后面跟细节（如 "start jobid=3839063"）
            arrow = "» %s" % (e.get("act") or "-")
            detail = str(e.get("note") or e.get("job") or "")
        elif e.get("ev") in ("finish", "fail") and e.get("dur") is not None:
            arrow = "%s（耗时 %s）" % (arrow, _fmt_dur(e.get("dur")))
        print("%-19s %-18s %-14s %-12s %-16s %s"
              % (str(e.get("ts") or "").replace("T", " ")[:19],
                 str(e.get("mat") or "")[:18], str(e.get("skill") or "")[:14],
                 str(e.get("step") or "")[:12], arrow,
                 str(detail)[:40]))
    by_mat = {}
    for e in evs:
        by_mat.setdefault(e.get("mat"), 0)
        by_mat[e["mat"]] += 1
    print("")
    top = sorted(by_mat.items(), key=lambda kv: -kv[1])[:5]
    print("按材料统计（前 5）：%s"
          % "，".join("%s %d 条" % (k, v) for k, v in top))
    if len(evs) > len(show):
        print("看更多：-n %d（或 --json 拿结构化数据）" % (len(evs) * 2))
    return 0
