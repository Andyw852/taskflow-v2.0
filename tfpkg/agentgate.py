# -*- coding: utf-8 -*-
"""agentgate —— LLM 动作网关 + 审计（v1.0 P0-1）。

**要解决的问题**：tf 分不清"人敲的"和"agent 敲的"。LLM 会话里一次手滑的
`tf rerun` 就会 rm -rf 掉算完的步骤目录，事后连"谁、什么时候、下的什么手"
都查不到（材料目录里的 tf.log 只记成功动作，不记被拒的调用、没有 actor）。

**做法**（两层，都不改既有命令的行为）：

1. **网关 `tf act <真实命令>`** —— agent 的唯一入口。命令按风险分三档：
   - `read`（list/summary/status/dir/skills/skill/schema/history/prove/probe/
     config/help/diagnose/session，以及任何带 `--dry-run` 的调用）：直接放行；
   - `mutate`（start/retry/fetch/init/adopt/level/hpc/auto/conf --set/correct/
     monitor…）：放行（本来就是 agent 该干的事）；
   - `destructive`（stop/rerun/clean/migrate-subdir/`correct -y`，以及任何带
     `-f`/`--force`/`-y`/`--yes`/`--purge-config` 的调用）：**必须人工批准**。
     人工在**交互终端**里跑一次 `tf approve <同一条命令>`（非 TTY 直接拒绝——
     agent 没法自己批准自己）；批准按"命令签名"记账，默认 15 分钟、一次用完即销。

2. **审计 `.tf_agent_log.jsonl`**（落在配置目录）：每次 `tf act` 调用都追加一条
   {ts, actor, cmd, argv, risk, decision, exit_code, dur, cwd, approved_by, …}
   ——放行、拒绝、失败都记。另外，agent 会话（环境变量 `TF_ACTOR` 已设）**直接**
   敲 tf 也会记一条（decision=direct），堵住"绕过网关就没人知道"。

**边界与开关**：
- 不设 `TF_ACTOR` 且不用 `tf act` → 行为与本改动前**完全一致**；
- 设了 `TF_ACTOR` → 直接调用也审计；破坏性动作是否要令牌由 `TF_AGENT_STRICT`
  决定（缺省：设了 TF_ACTOR 就严格；`TF_AGENT_STRICT=0` 可关，给用户自己的
  定时脚本留后门）；
- 只写配置目录下的两个文件，不碰材料目录、不碰超算、不发任何网络请求。

铁律对齐：stop/rerun/clean/-f/-y 本来就要人工同意（~/.dsh/AGENTS.md 第 2 节、
仓库 AGENTS.md 铁律 2）；这里把"口头同意"变成可审计的一次性令牌。
"""

import os
import sys
import time
import json
import hashlib
import datetime
import subprocess

# 审计与批准落盘位置（都在**配置目录**里，和 history.jsonl / .tf_hung.json 同级）
AGENT_LOG_NAME = ".tf_agent_log.jsonl"
AGENT_APPROVAL_NAME = ".tf_approvals.json"
AGENT_ACTOR_ENV = "TF_ACTOR"
AGENT_STRICT_ENV = "TF_AGENT_STRICT"
AGENT_GATEWAY_ENV = "TF_AGENT_GATEWAY"     # 网关子进程用它避免重复记账
AGENT_TTL_ENV = "TF_APPROVE_TTL"
AGENT_TTL_DEFAULT = 900                    # 批准有效期（秒）

# 三档风险：read / mutate / destructive
AGENT_READ_CMDS = {
    "list", "summary", "status", "json", "dir", "skills", "skill", "schema",
    "history", "prove", "probe", "config", "help", "diagnose", "session",
}
AGENT_MUTATE_CMDS = {
    "start", "retry", "fetch", "init", "adopt", "level", "hpc", "auto",
    "conf", "correct", "monitor", "watch", "restart", "push", "migrate-subdir",
}
AGENT_DESTRUCTIVE_CMDS = {"stop", "rerun", "clean", "migrate-subdir"}
AGENT_DESTRUCTIVE_FLAGS = {"-f", "--force", "-y", "--yes", "--purge-config"}

# 取值型选项：扫"命令词"时要跳过它们后面的值（tf act -p Si stop → stop 才是命令）
AGENT_VALUE_FLAGS = {
    "-p", "-j", "-job", "-tt", "-c", "--config", "-x", "--exclude",
    "-status", "--status", "-i", "--interval", "-n", "--since", "--limit",
    "--offset", "--host", "-u", "--user", "--set", "--out",
}


# ===== 基础工具 =====
def agent_actor():
    """当前动作的发起者：TF_ACTOR 优先；空串 = 不是 agent 会话。"""
    return (os.environ.get(AGENT_ACTOR_ENV) or "").strip()


def agent_strict():
    """破坏性动作要不要令牌。设了 TF_ACTOR 就默认严格；显式 0/false/off/关 可关。"""
    v = os.environ.get(AGENT_STRICT_ENV)
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() not in ("0", "false", "no", "off", "关", "否")


def agent_ttl():
    try:
        return max(1, int(os.environ.get(AGENT_TTL_ENV) or AGENT_TTL_DEFAULT))
    except (TypeError, ValueError):
        return AGENT_TTL_DEFAULT


def agent_cfg_dir(cfg=None):
    cfg = cfg or {}
    return (cfg.get("_config_dir")
            or (os.path.dirname(os.path.abspath(cfg["_config_path"]))
                if cfg.get("_config_path") else os.getcwd()))


def agent_log_path(cfg=None):
    return os.path.join(agent_cfg_dir(cfg), AGENT_LOG_NAME)


def agent_approval_path(cfg=None):
    return os.path.join(agent_cfg_dir(cfg), AGENT_APPROVAL_NAME)


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _now_epoch():
    return time.time()


# ===== 命令切分与风险判定 =====
def agent_split(raw_argv):
    """把 sys.argv[1:] 切成 (verb, inner, outer)。

    verb  = "act" / "approve" 出现的位置（第一个裸词）；
    inner = verb 之后的**原样** token（真实命令，含它的 -p/-j/-y…）；
    outer = verb 之前允许的全局选项（-c/--config、--host、-u/--user）。

    verb 之前若混进了 -p/-j/-tt/-f/-y 之类，直接拒绝——那会造成"批准的命令"
    与"执行的命令"不是同一条（`tf -p Si act stop` 里 -p 会被丢掉）。
    """
    toks = [str(x) for x in (raw_argv or [])]
    idx = None
    for i, t in enumerate(toks):
        if t in ("act", "approve"):
            idx = i
            break
    if idx is None:
        return None, list(toks), []
    outer, i = [], 0
    keep_pair = {"-c", "--config", "--host", "-u", "--user"}
    while i < idx:
        t = toks[i]
        if t in keep_pair and i + 1 < idx:
            outer += [t, toks[i + 1]]
            i += 2
            continue
        if not t.startswith("-"):
            return ("act-error", list(toks[idx + 1:]), outer)
        i += 1
    return toks[idx], list(toks[idx + 1:]), outer


def agent_command(tokens):
    """从 token 流里挑出真正的命令词（跳过取值型选项的值）。"""
    skip = False
    for t in tokens or []:
        if skip:
            skip = False
            continue
        if t.startswith("-"):
            base = t.split("=", 1)[0]
            if "=" not in t and base in AGENT_VALUE_FLAGS:
                skip = True
            continue
        return t
    return None


def agent_classify(cmd, argv=()):
    """返回 (risk, why)：risk ∈ read / mutate / destructive，why 是中文理由清单。"""
    argv = [str(x) for x in (argv or [])]
    flags = set()
    for x in argv:
        if x.startswith("-"):
            flags.add(x.split("=", 1)[0])
    if "--dry-run" in flags:
        return "read", ["--dry-run 排练：只打印将影响的对象，无副作用"]
    if cmd in AGENT_DESTRUCTIVE_CMDS:
        return "destructive", ["%s 会取消作业 / 删除已算产物" % cmd]
    hit = sorted(flags & AGENT_DESTRUCTIVE_FLAGS)
    if hit:
        return "destructive", ["带强制/免确认开关 %s" % " ".join(hit)]
    if cmd == "conf" and "--set" not in flags:
        return "read", ["conf 不带 --set：只读展示"]
    if cmd in AGENT_MUTATE_CMDS:
        return "mutate", ["%s 会生成输入/推进状态" % cmd]
    if cmd in AGENT_READ_CMDS:
        return "read", ["只读命令"]
    return "mutate", ["未知命令，按非破坏性动作记账放行"]


def agent_signature(cmd, argv):
    """命令签名：对"命令词 + 参数（忽略顺序与 -y/--yes）"取 sha256 前 12 位。

    人工 `tf approve` 与 agent `tf act` 各算一次，参数顺序不同也能对上；
    -y/--yes 只是"我知道自己在干什么"，不参与签名。
    """
    toks = [str(x) for x in (argv or []) if str(x) not in ("-y", "--yes")]
    payload = json.dumps({"cmd": cmd, "args": sorted(toks)},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def agent_targets(argv):
    """从参数里抠出材料/步骤/技能，便于审计与按材料过滤。"""
    out = {"mat": None, "step": None, "tt": None, "host": None}
    key = {"-p": "mat", "--proj": "mat", "-tt": "tt",
           "-j": "step", "-job": "step", "--host": "host"}
    toks = [str(x) for x in (argv or [])]
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in key and i + 1 < len(toks):
            out[key[t]] = toks[i + 1]
            i += 2
            continue
        i += 1
    return out


# ===== 审计日志 =====
def agent_audit(cfg, actor, cmd, argv, risk, decision, why=None,
                exit_code=None, dur=None, approved_by=None, sig=None,
                gateway="act", ev="call", note=None):
    """追加一条审计记录。整段包 try/except：审计失败绝不影响命令本身。"""
    rec = {
        "ts": _now(),
        "ev": ev,
        "actor": actor or "",
        "cmd": cmd or "",
        "argv": list(argv or []),
        "risk": risk or "",
        "gateway": gateway,
        "decision": decision,
        "exit_code": exit_code,
        "dur": round(dur, 2) if isinstance(dur, (int, float)) else None,
        "cwd": os.getcwd(),
        "approved_by": approved_by,
        "sig": sig,
        "why": list(why or []),
    }
    rec.update({k: v for k, v in agent_targets(argv).items() if v})
    if note:
        rec["note"] = str(note)[:200]
    try:
        path = agent_log_path(cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as _e:                       # noqa: BLE001
        print("警告：审计日志写不进去（忽略）：%s" % _e, file=sys.stderr)
    return rec


def agent_log_load(cfg, proj=None, since=None, risk=None, limit=None):
    """读审计日志并按材料/时间/风险过滤。返回 (记录列表, 文件总行数)。"""
    path = agent_log_path(cfg)
    out, total = [], 0
    if not os.path.isfile(path):
        return out, 0
    wants = {x.strip() for x in str(proj or "").split(",") if x.strip()}
    since = str(since or "").strip().replace(" ", "T")
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
                if wants:
                    mat = str(e.get("mat") or "")
                    if not (mat in wants or os.path.basename(mat) in wants):
                        continue
                if since and str(e.get("ts") or "") < since:
                    continue
                if risk and e.get("risk") != risk:
                    continue
                out.append(e)
    except OSError:
        return [], 0
    if limit:
        out = out[-int(limit):]
    return out, total


# ===== 人工批准（一次性令牌）=====
def _approvals_read(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _approvals_write(path, d):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def agent_save_approval(cfg, sig, cmd, argv, by, ttl=None):
    """人工批准一条命令签名（一次性、带有效期）。"""
    path = agent_approval_path(cfg)
    d = _approvals_read(path)
    items = [x for x in (d.get("approvals") or [])
             if not x.get("used")
             and float(x.get("expires_epoch") or 0) > _now_epoch()]
    items.append({"sig": sig, "cmd": cmd, "argv": list(argv or []),
                  "by": by or "", "ts": _now(), "used": False,
                  "expires_epoch": _now_epoch() + float(ttl or agent_ttl())})
    d["approvals"] = items
    d["updated"] = _now()
    return _approvals_write(path, d)


def agent_take_approval(cfg, sig):
    """取用一条尚未用过的批准（用完置 used，防止重放）。返回 (ok, by)。"""
    path = agent_approval_path(cfg)
    d = _approvals_read(path)
    items = list(d.get("approvals") or [])
    now = _now_epoch()
    hit = None
    for x in items:
        if (not x.get("used") and x.get("sig") == sig
                and float(x.get("expires_epoch") or 0) > now):
            hit = x
            break
    if hit is None:
        return False, None
    hit["used"] = True
    hit["used_at"] = _now()
    d["approvals"] = items
    _approvals_write(path, d)
    return True, hit.get("by")


def agent_pending_approvals(cfg):
    d = _approvals_read(agent_approval_path(cfg))
    now = _now_epoch()
    return [x for x in (d.get("approvals") or [])
            if not x.get("used")
            and float(x.get("expires_epoch") or 0) > now]


# ===== 渲染 =====
AGENT_RISK_CN = {"read": "只读", "mutate": "推进", "destructive": "破坏性"}
AGENT_POLICY_ROWS = [
    ("read", "list summary status json dir skills skill schema history prove "
             "probe config help diagnose session / 任何 --dry-run", "放行"),
    ("mutate", "start retry fetch init adopt level hpc auto conf --set correct "
               "monitor restart push", "放行（记账）"),
    ("destructive", "stop rerun clean migrate-subdir / 任何 -f -y --purge-config",
     "需人工批准令牌"),
]


def _wrap_words(text, width=78):
    """按空格把长串折行（纯展示用）。"""
    out, cur = [], ""
    for w in str(text).split():
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out or [""]


def render_agent_policy():
    L = ["tf act —— agent 动作网关（v1.0 P0-1）", ""]
    for risk, cmds, policy in AGENT_POLICY_ROWS:
        L.append("【%s】 %s" % (AGENT_RISK_CN.get(risk, risk), policy))
        L.append("    命令：%s" % _wrap_words(cmds, 74)[0])
        for extra in _wrap_words(cmds, 74)[1:]:
            L.append("          %s" % extra)
    L.append("")
    L.append("用法：")
    L.append("  tf act <和 tf 一模一样的命令>     # agent 的唯一入口（自动记账）")
    L.append("  tf approve <同一条命令>           # 人工在交互终端批准破坏性动作")
    L.append("  tf act log [-n 40] [--json]       # 看审计流水")
    L.append("  tf act policy                     # 看这张表")
    L.append("")
    L.append("说明：批准按「命令签名」记账（同一命令、参数顺序无关），默认 %d 秒内一次有效；"
             % AGENT_TTL_DEFAULT)
    L.append("      非交互终端（管道 / agent 子进程 / 定时任务）不能执行 tf approve")
    L.append("      ——agent 无法自我批准。")
    L.append("      环境变量：TF_ACTOR=名字（谁在操作）、TF_AGENT_STRICT=0（关令牌）、")
    L.append("                TF_APPROVE_TTL=秒（批准有效期）。")
    return "\n".join(L)


def agent_render_log(cfg, n=40, json_out=False, proj=None, since=None):
    path = agent_log_path(cfg)
    evs, total = agent_log_load(cfg, proj=proj, since=since)
    if json_out:
        print(json.dumps({"path": path, "total": total, "count": len(evs),
                          "events": evs[-int(n or 40):]},
                         ensure_ascii=False, indent=2))
        return 0
    if total == 0:
        print("还没有审计记录（%s 不存在或为空）。" % path)
        print("agent 侧：tf act <命令> 自动记一条；设了 TF_ACTOR 的直接调用也记。")
        return 0
    show = evs[-int(n or 40):]
    print("agent 审计  %s" % path)
    print("共 %d 条（文件累计 %d 条），显示最近 %d 条：" % (len(evs), total, len(show)))
    print("")
    print("%-19s %-10s %-9s %-11s %-24s %-6s %s"
          % ("时间", "actor", "风险", "决定", "命令", "退出码", "批准人"))
    for e in show:
        argv = " ".join(str(x) for x in (e.get("argv") or []))
        line = ("%s %s" % (e.get("cmd") or "?", argv)).strip()
        print("%-19s %-10s %-9s %-11s %-24s %-6s %s"
              % (str(e.get("ts") or "").replace("T", " ")[:19],
                 str(e.get("actor") or "")[:10],
                 AGENT_RISK_CN.get(e.get("risk"), str(e.get("risk") or ""))[:9],
                 str(e.get("decision") or "")[:11],
                 line[:24],
                 "-" if e.get("exit_code") is None else e.get("exit_code"),
                 str(e.get("approved_by") or "-")))
    pend = agent_pending_approvals(cfg)
    if pend:
        print("")
        print("尚有 %d 条未使用的批准（签名 %s）。"
              % (len(pend), ", ".join(str(x.get("sig")) for x in pend)))
    if len(evs) > len(show):
        print("看更多：-n %d（或 --json 拿结构化数据）" % (len(evs) * 2))
    return 0


def agent_deny_message(cfg, cmd, inner, sig, why, prog=None):
    prog = prog or "bin/tf"
    L = ["✗ 拒绝执行：tf %s %s 属**破坏性动作**，agent 不能自行决定（AGENTS.md 铁律 2）。"
         % (cmd, " ".join(str(x) for x in inner))]
    if why:
        L.append("  理由：%s" % "；".join(why))
    L.append("")
    L.append("  请**人工**在交互终端里批准（批准后一次有效，默认 %d 秒）："
             % agent_ttl())
    L.append("      python3 %s approve %s" % (prog, " ".join(str(x) for x in inner)))
    L.append("")
    L.append("  命令签名：%s（参数顺序无关；改了任何一个参数都要重新批准）" % sig)
    L.append("  审计日志：%s" % agent_log_path(cfg))
    return "\n".join(L)


# ===== 命令入口 =====
def cmd_act(cfg, raw_argv):
    """tf act <真实命令> —— agent 的唯一入口：判定风险 + 记账 + 转发。

    返回子进程退出码（放行）或 3（拒绝）。
    """
    from tfpkg import _PKG_ROOT
    verb, inner, outer = agent_split(raw_argv)
    if verb == "act-error":
        print("错误：act 之前的选项里混进了 -p/-j/-tt/-f/-y 这类会影响目标的参数。\n"
              "      请把它们写在 act 之后（否则批准的命令和执行的命令不是同一条）：\n"
              "        tf act -p <材料> [-j <步骤>] <命令>")
        return 2
    if not inner:
        print(render_agent_policy())
        return 0
    sub = agent_command(inner)
    if sub == "policy":
        print(render_agent_policy())
        return 0
    if sub == "log":
        return agent_render_log(cfg, n=40, json_out=False)
    if sub in ("act", "approve"):
        print("错误：act/approve 不能嵌套（网关只此一层）。")
        return 2
    if sub is None:
        print(render_agent_policy())
        return 0
    risk, why = agent_classify(sub, inner)
    actor = agent_actor() or os.environ.get("USER") or "?"
    sig = agent_signature(sub, inner)
    approved_by = None
    prog = os.path.join(_PKG_ROOT, "bin", "tf")
    if risk == "destructive":
        ok, approver = agent_take_approval(cfg, sig)
        if not ok:
            agent_audit(cfg, actor, sub, inner, risk, "deny-need-approval",
                        why=why, exit_code=3, sig=sig, gateway="act")
            print(agent_deny_message(cfg, sub, inner, sig, why, prog=prog))
            return 3
        approved_by = approver or "human"
    child = [sys.executable, prog] + outer + inner
    env = dict(os.environ)
    env[AGENT_GATEWAY_ENV] = "act"        # 子进程不再重复记账/重复要令牌
    t0 = time.time()
    try:
        rc = subprocess.call(child, env=env)
    except OSError as _e:
        agent_audit(cfg, actor, sub, inner, risk, "error", why=why, exit_code=127,
                    sig=sig, approved_by=approved_by, note=str(_e), gateway="act")
        print("错误：无法执行 tf 子进程：%s" % _e)
        return 127
    dur = time.time() - t0
    agent_audit(cfg, actor, sub, inner, risk, "allow", why=why, exit_code=rc,
                dur=dur, sig=sig, approved_by=approved_by, gateway="act")
    return rc


def cmd_approve(cfg, raw_argv):
    """tf approve <命令> —— 人工批准一条破坏性命令（必须在交互终端里跑）。"""
    verb, inner, outer = agent_split(raw_argv)
    if verb == "act-error" or not inner:
        print("用法：tf approve -p <材料> [-j <步骤>] <破坏性命令>\n"
              "      例：tf approve -p C24/qHPC24 clean")
        return 2
    sub = agent_command(inner)
    if sub is None:
        print("用法：tf approve -p <材料> [-j <步骤>] <破坏性命令>")
        return 2
    risk, why = agent_classify(sub, inner)
    if risk != "destructive":
        print("提示：tf %s 属「%s」档，本来就不需要批准（approve 只管破坏性动作）。"
              % (sub, AGENT_RISK_CN.get(risk, risk)))
        return 0
    if not sys.stdin.isatty():
        print("✗ 拒绝：批准必须在**交互终端**里做。\n"
              "  非交互（管道 / agent 子进程 / 定时任务）一律不接受——"
              "这条正是为了防止 agent 自己批准自己。")
        return 3
    sig = agent_signature(sub, inner)
    print("即将批准一次破坏性动作：")
    print("    命令    tf %s" % " ".join(str(x) for x in inner))
    print("    理由    %s" % "；".join(why))
    print("    签名    %s" % sig)
    print("    有效期  %d 秒（一次有效，用完即销）" % agent_ttl())
    try:
        ans = input("确认批准？[y/N] ")
    except EOFError:
        print("（没有输入，未批准。）")
        return 1
    if str(ans).strip().lower() not in ("y", "yes", "是", "好"):
        print("（已取消，未批准。）")
        return 1
    by = os.environ.get("USER") or "human"
    if not agent_save_approval(cfg, sig, sub, inner, by):
        print("✗ 批准写不进 %s（检查配置目录权限）。" % agent_approval_path(cfg))
        return 1
    agent_audit(cfg, by, sub, inner, risk, "approved-by-human", why=why,
                sig=sig, approved_by=by, gateway="approve", ev="approve")
    print("✓ 已批准（签名 %s，%d 秒内一次有效）。\n"
          "  agent 现在可以把**同一条命令**用 tf act 重跑。" % (sig, agent_ttl()))
    return 0


def agent_direct_gate(cfg, cmd, raw_argv):
    """agent 会话（TF_ACTOR 已设）**直接**敲 tf 时的旁路钩子。

    返回 None（放行）或非零退出码（拒绝）。网关子进程 / 非 agent 会话直接放行，
    所以对现有用法是**零影响**。
    """
    if os.environ.get(AGENT_GATEWAY_ENV) == "act":
        return None
    actor = agent_actor()
    if not actor:
        return None
    if cmd in ("act", "approve"):
        return None
    inner = [str(x) for x in (raw_argv or [])]
    risk, why = agent_classify(cmd, inner)
    sig = agent_signature(cmd, inner)
    if risk == "destructive" and agent_strict():
        ok, approver = agent_take_approval(cfg, sig)
        if not ok:
            agent_audit(cfg, actor, cmd, inner, risk, "deny-need-approval",
                        why=why, exit_code=3, sig=sig, gateway="direct")
            print("✗ 拒绝执行：agent 会话（TF_ACTOR=%s）直接执行破坏性命令 tf %s。\n"
                  "  请走网关：tf act %s\n"
                  "  人工批准：tf approve %s"
                  % (actor, cmd, " ".join(inner), " ".join(inner)))
            return 3
        agent_audit(cfg, actor, cmd, inner, risk, "allow-direct-approved",
                    why=why, sig=sig, approved_by=approver or "human",
                    gateway="direct")
        return None
    agent_audit(cfg, actor, cmd, inner, risk, "direct", why=why, sig=sig,
                gateway="direct")
    return None
