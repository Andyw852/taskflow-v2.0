# -*- coding: utf-8 -*-
"""prov.py —— 每一步"结果是怎么来的"档案（provenance.json）

论文/审稿人最常问的一句是"这个数怎么来的"。taskflow 里原来只有零散的
workflow_method.txt / POSCAR.provenance / mu_provenance，覆盖不全、格式不一。
本模块统一成一份**机器可读**的 provenance.json：

  谁写的      什么时候      用什么技能/版本      哪一步
  输入文件 sha256（gen 时推送的每个文件：POSCAR / 模板 / 公共库 / step.conf）
  step.conf 合并后的最终参数（可复现的关键）
  hpc / 登录节点 / 远端步骤目录 / tf 版本
  （可选，由 skill/_common/provenance_common.py 在作业里补）
  工具版本（vasp / mace / phono3py / pheasy …）、产物 sha256、作业号、墙钟时间

设计原则：**只加不减、永不阻断计算**。
  · gen 推送阶段由 tf 自己写（写失败只警告，不影响 gen；开关 tf.yaml 的
    provenance: false 或环境变量 TF_PROVENANCE=0 可整体关掉）
  · 作业/脚本侧可选补写（provenance_common.py 合并字段，不覆盖 tf 写的）
  · 只读读取：tf prove -p 材料 [-j 步骤]（读本地 result/ 里回拉的那份）
"""
import hashlib
import json
import os
import time

PROV_NAME = "provenance.json"
PROV_DIR = "provenance"        # 远端 <材料>/provenance/<步骤>.json（tf 自动写）
PROV_SCHEMA = 1
_MAX_CONF_TEXT = 20000      # step.conf 原文最多存这么多字符（够复现，不会撑爆）


# ===== 基础工具 =====
def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, limit=None):
    """文件 sha256；读不了返回 None（绝不抛异常）。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                b = fh.read(1 << 20)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except OSError:
        return None


def provenance_enabled(cfg):
    """开关：环境变量 TF_PROVENANCE=0 关；否则看 tf.yaml 的 provenance（缺省开）。"""
    env = str(os.environ.get("TF_PROVENANCE", "")).strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    val = (cfg or {}).get("provenance")
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off")
    return True if val is None else bool(val)


def _step_conf_summary(cfg, t, m, sname):
    """step.conf 合并后的最终参数（复现的关键）。拿不到就返回 None。"""
    try:
        from tfpkg import build_step_conf, STEP_CONF
        text, _lg = build_step_conf(cfg, t, m, sname)
    except Exception:
        return None
    if not text:
        return None
    out = {"name": STEP_CONF, "sha256": sha256_bytes(text.encode("utf-8")),
           "chars": len(text)}
    out["text"] = text if len(text) <= _MAX_CONF_TEXT else text[:_MAX_CONF_TEXT]
    if len(text) > _MAX_CONF_TEXT:
        out["truncated"] = True
    return out


def build_gen_provenance(cfg, t, m, sname, files, host=None, gen_script=None,
                         step_dir=None, compact=False):
    """gen 推送阶段生成 provenance.json 的**内容字符串**。

    files: {远端文件名: {"sha256":..., "source": 本地路径 或 null, "origin": "skill"/"project"/"gen_dir"}}
    任何异常由调用方兜住——档案不该影响计算。"""
    from tfpkg import TF_VERSION
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    prov = {
        "schema": PROV_SCHEMA,
        "kind": "gen",
        "ts": now,
        "tf_version": TF_VERSION,
        "skill": {
            "key": t.get("key") if isinstance(t, dict) else None,
            "version": (t or {}).get("_skill_version"),
            "manifest": (t or {}).get("_skill_manifest"),
            "desc": (t or {}).get("desc"),
        },
        "step": sname,
        "material": {"name": (m or {}).get("name"),
                     "path": (m or {}).get("path"),
                     "local": (m or {}).get("lpath")},
        "hpc": (m or {}).get("hpc_name"),
        "host": host,
        "step_dir": step_dir,
        "generator": {
            "script": gen_script,
            "sha256": (files.get(gen_script) or {}).get("sha256"),
            "args": (files.get(gen_script) or {}).get("args"),
        },
        "inputs": {k: v for k, v in sorted(files.items())},
        "step_conf": _step_conf_summary(cfg, t, m, sname),
        "run": {
            "actor": os.environ.get("TF_ACTOR") or os.environ.get("USER") or "",
            "cwd": os.getcwd(),
            "argv": " ".join(__import__("sys").argv[:1]),
        },
    }
    if compact:                      # 时间线用：一行一条，便于 append 与 grep
        return json.dumps(prov, ensure_ascii=False, sort_keys=False)
    return json.dumps(prov, ensure_ascii=False, indent=2, sort_keys=False)


# ===== 读取与展示 =====
def read_provenance(path):
    """读一份 provenance.json；不存在/坏了返回 None（不抛）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def local_provenance_paths(m, sname=None, label=None, step_dirname=None):
    """本地可能的 provenance.json 位置（fetch 回拉后）：result/<步骤>/provenance.json 等。"""
    out = []
    rd = (m or {}).get("result_dir")
    names = [x for x in (sname, label, step_dirname) if x]
    if rd:
        for n in names:
            out.append(os.path.join(rd, str(n), PROV_NAME))
        out.append(os.path.join(rd, PROV_NAME))
        for n in names:      # 有些技能会把 <材料>/provenance/ 整个目录拉回来
            out.append(os.path.join(rd, PROV_DIR, "%s.json" % n))
    lp = (m or {}).get("lpath")
    if lp:
        for n in names:
            out.append(os.path.join(lp, str(n), PROV_NAME))
    return out


def find_local_provenance(m, **kw):
    for p in local_provenance_paths(m, **kw):
        if os.path.isfile(p):
            return p, read_provenance(p)
    return None, None


def provenance_summary(prov):
    """一行摘要（表格用）。"""
    if not prov:
        return "-"
    ins = prov.get("inputs") or {}
    tools = prov.get("tools") or {}
    tv = []
    if isinstance(tools, dict):
        for k, v in tools.items():
            ver = (v or {}).get("version") if isinstance(v, dict) else v
            tv.append("%s %s" % (k, ver) if ver else str(k))
    bits = ["%d 输入" % len(ins)]
    if tv:
        bits.append(" ".join(tv[:3]))
    if prov.get("job_id"):
        bits.append("job %s" % prov["job_id"])
    if prov.get("ts"):
        bits.append(str(prov["ts"])[:19])
    return " · ".join(bits)


def verify_provenance(prov, base_dirs=()):
    """校验：档案里记的输入 sha256，和现在磁盘上的文件是否还一致。

    返回 [(名字, 'ok'|'changed'|'missing', 档案sha, 现在sha)]。只在能找到文件时判，
    找不到就跳过（远端文件本地没有是常态，不算问题）。"""
    out = []
    for name, meta in sorted((prov.get("inputs") or {}).items()):
        if not isinstance(meta, dict) or not meta.get("sha256"):
            continue
        now = None
        for d in base_dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                now = sha256_file(p)
                break
        if now is None:
            out.append((name, "missing", meta.get("sha256"), None))
        elif now == meta.get("sha256"):
            out.append((name, "ok", meta.get("sha256"), now))
        else:
            out.append((name, "changed", meta.get("sha256"), now))
    return out


def render_provenance(rows):
    """tf prove 的文本输出。rows = [{"material","skill","step","path","prov"}...]"""
    L = []
    if not rows:
        L.append("没有找到任何 provenance.json。")
        L.append("说明：档案在**生成输入时**由 tf 自动写进远端步骤目录，随 fetch 回拉本地")
        L.append("      （result/<步骤>/provenance.json）。跑过 tf start/init 的步骤才有。")
        return "\n".join(L)
    for r in rows:
        prov = r.get("prov") or {}
        L.append("=" * 78)
        L.append("%s  %s  %s" % (r.get("material"), r.get("skill"), r.get("step")))
        L.append("  档案   %s" % r.get("path"))
        sk = prov.get("skill") or {}
        L.append("  生成   %s  tf %s  技能 %s v%s"
                 % (prov.get("ts") or "?", prov.get("tf_version") or "?",
                    sk.get("key") or "?", sk.get("version") or "?"))
        L.append("  环境   hpc=%s host=%s" % (prov.get("hpc"), prov.get("host")))
        if prov.get("step_dir"):
            L.append("  远端   %s" % prov["step_dir"])
        gen = prov.get("generator") or {}
        if gen.get("script"):
            L.append("  生成器 %s  sha256 %s"
                     % (gen.get("script"), (gen.get("sha256") or "?")[:12]))
        ins = prov.get("inputs") or {}
        if ins:
            L.append("  输入   %d 个文件（sha256 前 12 位）" % len(ins))
            for k in sorted(ins):
                v = ins[k] or {}
                L.append("    %-24s %s%s" % (k, (v.get("sha256") or "-")[:12],
                                             ("  ← " + v["source"]) if v.get("source") else ""))
        sc = prov.get("step_conf") or {}
        if sc:
            L.append("  参数   step.conf sha256 %s（%d 字符%s）"
                     % ((sc.get("sha256") or "?")[:12], sc.get("chars") or 0,
                        "，已截断" if sc.get("truncated") else ""))
        tools = prov.get("tools") or {}
        if tools:
            L.append("  工具   %s" % ", ".join(
                "%s=%s" % (k, (v or {}).get("version") if isinstance(v, dict) else v)
                for k, v in sorted(tools.items())))
        outs = prov.get("outputs") or {}
        if outs:
            L.append("  产物   %s" % ", ".join(
                "%s(%s)" % (k, ((v or {}).get("sha256") or "-")[:12])
                for k, v in sorted(outs.items())))
        for k in ("job_id", "wall_sec", "exit_code", "note"):
            if prov.get(k) is not None:
                L.append("  %-6s %s" % (k, prov.get(k)))
    L.append("=" * 78)
    L.append("共 %d 份档案。逐份校验输入是否被改过：tf prove -p <材料> --verify"
             % len(rows))
    return "\n".join(L)


# ===== 命令入口：tf prove =====
def cmd_prove(cfg, data, proj, job=None, json_out=False, verify=False):
    """tf prove -p <材料> [-j <步骤>] [--json] [--verify]
    只读：从本地 result/ 读回拉的 provenance.json，打印"这一步怎么来的"。"""
    from tfpkg import find_material, find_step
    t, m = find_material(data, proj)
    if m is None or t is None:
        print("错误：找不到材料 %s。" % proj)
        return 1
    steps = m.get("steps") or []
    if job:
        steps = [find_step(m, job)]
    rows = []
    for s in steps:
        name = s.get("name")
        dirname = os.path.basename(str(s.get("dir") or "").rstrip("/")) or None
        path, prov = find_local_provenance(m, sname=name, label=s.get("label"),
                                           step_dirname=dirname)
        if not prov:
            continue
        row = {"material": m.get("name"), "skill": t.get("key"), "step": name,
               "label": s.get("label"), "path": path, "prov": prov}
        if verify:
            base = [os.path.dirname(path)]
            row["verify"] = verify_provenance(prov, base)
        rows.append(row)
    if json_out:
        print(json.dumps({"material": m.get("name"), "skill": t.get("key"),
                          "count": len(rows), "items": rows}, ensure_ascii=False, indent=2))
        return 0
    print(render_provenance(rows))
    if verify:
        for r in rows:
            bad = [x for x in r.get("verify") or [] if x[1] != "ok"]
            if not bad:
                print("校验 %s %s：输入全部未变 ✓" % (r["material"], r["step"]))
            else:
                for name, st, a, b in bad:
                    print("校验 %s %s：%s %s（档案 %s → 现在 %s）"
                          % (r["material"], r["step"], name, st,
                             (a or "-")[:12], (b or "-")[:12]))
    return 0
