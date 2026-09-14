# -*- coding: utf-8 -*-
"""session —— 会话导出（v1.0 P1-7）。

**要解决的问题**：论文的"可复现性"补充材料，现在得手工去超算拷 result/、翻
setting/history.jsonl、翻 monitor 日志、再自己写说明——每篇文章重来一遍，而且
很容易漏（"这步的输入是怎么生成的"往往就说不清）。

**做法**：`tf session export -p <材料>` 把**一个材料**的完整"操作与来路"打成一个
tar.gz（默认落 cwd 的 tmp/）：

  manifest.json          机器可读总账：tf 版本 / git 提交 / 时间 / 材料 / 技能 /
                         超算 / 每步状态 / 文件清单（逐个 sha256 + 字节数）
  README.txt             这份包里有什么、怎么读、怎么重放（给人看）
  status.txt             导出时刻的步骤状态（label / kind / 诊断 / 作业号）
  history.jsonl          该材料的状态转移 + 动作事件 + 完成耗时（tf history 的数据源）
  agent_log.jsonl        LLM 动作审计（tf act 的流水；没有就不放）
  provenance/<步骤>.json  每步"输入 sha256 / step.conf 参数 / 工具版本 / 作业号"

**边界**：全程只读本地文件（不连超算、不提交、不改任何东西）；provenance 只收
已经 fetch 回本地的那些（要更全先跑 `tf -p <材料> fetch`）。包里不含任何口令/密钥，
也不含 config 的敏感段（只记配置路径 + host + work_dir）。`--json` 只打印总账、不写包。

用法：
  tf session export -p Si_auto [--out 路径.tar.gz] [--since 7d] [--json]
"""

import os
import io
import sys
import json
import time
import tarfile
import hashlib
import datetime

SESSION_SCHEMA = 1
SESSION_README = """# 会话导出包（taskflow session export）

这个包是材料 **%(mat)s**（技能 %(skill)s，超算 %(host)s）在 %(created)s 的
"操作与来路"快照，可直接作为论文的**可复现性补充材料**。

## 包里有什么

| 文件 | 内容 |
|---|---|
| manifest.json | 机器可读总账：tf 版本 / git 提交 / 每步状态 / 文件清单（含 sha256） |
| status.txt | 导出时刻的步骤状态（label、状态、诊断、作业号、远端目录） |
| history.jsonl | 状态转移 + 动作事件（谁在什么时候 init/start/retry/stop/fetch）+ 完成耗时 |
| agent_log.jsonl | LLM 动作审计流水（谁、什么命令、什么风险档、是否人工批准、退出码） |
| provenance/*.json | 每步的输入文件 sha256、step.conf 合并后的参数、生成器脚本哈希、作业号 |

## 怎么读

```bash
python3 -c "import json;m=json.load(open('manifest.json'));print(m['material'],m['skill'],m['counts'])"
tail -20 history.jsonl                                   # 事件流（JSONL，每行一条）
python3 -m json.tool provenance/S1_opt.json | head -40    # 某一步的来路
```

## 怎么重放

1. 把材料目录根的结构文件（POSCAR）放回项目根，跑 `tf -p %(mat)s init`；
2. 按 provenance 里的参数逐字段核对生成输入：`tf -p %(mat)s -j <步骤> init`；
3. 用 `tf prove -p %(mat)s --verify` 逐字节校验输入是否与档案一致。

> 说明：包由 `tf session export` 生成，只读本地已回拉的数据；远端仍有而本地
> 未 fetch 的产物不在此包内（要收全先跑 `tf -p %(mat)s fetch`）。
"""


def _session_git(root):
    """尽力取 git 提交号（非仓库 / 无 git 时返回空 dict，不报错）。"""
    import subprocess as _sp
    out = {}
    try:
        r = _sp.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            out["rev"] = r.stdout.strip()
        r = _sp.run(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            out["branch"] = r.stdout.strip()
        r = _sp.run(["git", "-C", root, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            out["dirty_files"] = len([x for x in r.stdout.splitlines() if x.strip()])
    except (OSError, _sp.SubprocessError):
        pass
    return out


def _session_step_lines(m):
    L = []
    for s in (m.get("steps") or []):
        job = s.get("job") or {}
        job_txt = "-"
        if isinstance(job, dict) and job:
            job_txt = "%s %s" % (job.get("id") or "?", job.get("state") or "")
            if job.get("info"):
                job_txt += " (%s)" % job["info"]
        L.append("%-14s %-12s %-9s %-40s %s"
                 % (str(s.get("label") or "")[:14], str(s.get("name") or "")[:12],
                    str(s.get("kind") or "")[:9], str(s.get("diag") or "-")[:40],
                    job_txt))
    return L


def _session_collect(cfg, data, proj, since=None):
    """把一个材料的导出内容聚起来（纯本地读取）。返回 (manifest, files)。"""
    from tfpkg import (TF_VERSION, _PKG_ROOT, find_material,
                       find_local_provenance, history_load, agent_log_load)
    t, m = find_material(data, proj)
    mat = m.get("name")
    skill = t.get("key")
    host = m.get("host_eff") or cfg.get("host")
    files = {}

    # 1) 事件流（状态转移 + 动作 + 完成耗时）
    evs, _tot = history_load(cfg, proj=mat, since=since)
    files["history.jsonl"] = "".join(
        json.dumps(e, ensure_ascii=False) + "\n" for e in evs)

    # 2) agent 审计（P0-1 的 tf act 流水）
    ags, _atot = agent_log_load(cfg, proj=mat, since=since)
    agent_txt = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in ags)
    if agent_txt:
        files["agent_log.jsonl"] = agent_txt

    # 3) 每步 provenance（本地已回拉的部分）
    provs = {}
    for s in (m.get("steps") or []):
        dirname = os.path.basename(str(s.get("dir") or "").rstrip("/")) or None
        path, prov = find_local_provenance(m, sname=s.get("name"),
                                           label=s.get("label"),
                                           step_dirname=dirname)
        if prov:
            provs[str(s.get("name"))] = prov
            files["provenance/%s.json" % s.get("name")] = json.dumps(
                prov, ensure_ascii=False, indent=2) + "\n"

    # 4) 状态快照（人看）
    _sd = next((s.get("dir") for s in (m.get("steps") or []) if s.get("dir")), None)
    st = ["材料      %s" % mat,
          "技能      %s" % skill,
          "超算      %s" % (host or "-"),
          "远端目录  %s" % (os.path.dirname(str(_sd).rstrip("/")) if _sd else "-"),
          "本地结果  %s" % (m.get("result_dir") or "-"),
          "导出时间  %s" % datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
          "tf 版本   %s" % TF_VERSION,
          "",
          "%-14s %-12s %-9s %-40s %s"
          % ("步骤", "目录名", "状态", "诊断", "作业")]
    st += _session_step_lines(m)
    files["status.txt"] = "\n".join(st) + "\n"

    counts = {"history_events": len(evs),
              "action_events": len([e for e in evs if e.get("ev") == "action"]),
              "finish_events": len([e for e in evs
                                    if e.get("ev") in ("finish", "fail")]),
              "agent_calls": len(ags),
              "agent_denied": len([e for e in ags
                                   if str(e.get("decision") or "").startswith("deny")]),
              "provenance_steps": len(provs)}
    manifest = {
        "schema_version": SESSION_SCHEMA,
        "kind": "taskflow-session-export",
        "created": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "material": mat,
        "skill": skill,
        "host": host,
        "tf_version": TF_VERSION,
        "git": _session_git(_PKG_ROOT),
        "config": cfg.get("_config_path"),
        "work_dir": (cfg.get("task_types", {}).get(skill) or {}).get("work_dir"),
        "steps": [{"label": s.get("label"), "name": s.get("name"),
                   "kind": s.get("kind"), "diag": s.get("diag"),
                   "job": (s.get("job") or {}).get("id") if s.get("job") else None,
                   "job_state": (s.get("job") or {}).get("state")
                   if s.get("job") else None}
                  for s in (m.get("steps") or [])],
        "provenance": sorted(provs.keys()),
        "counts": counts,
        "files": [],
        "replay": {
            "init": "tf -p %s init" % mat,
            "gen": "tf -p %s -j <步骤> init  # 按 provenance 参数逐字段核对" % mat,
            "verify": "tf prove -p %s --verify" % mat,
            "note": "本包只含本地已回拉的数据；远端未 fetch 的产物不在内。",
        },
    }
    return manifest, files


def _session_sha(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def cmd_session(cfg, data, proj, job=None, out=None, since=None,
                json_out=False, write=True):
    """tf session export -p <材料> [--out 文件] [--since 7d] [--json]"""
    if not proj:
        print("用法：tf session export -p <材料> [--out 路径.tar.gz] [--since 7d] [--json]")
        print("      把该材料的操作历史 + provenance + agent 审计打成一个包，"
              "做论文的可复现性补充材料。")
        return 2
    try:
        manifest, files = _session_collect(cfg, data, proj, since=since)
    except SystemExit as _e:                    # find_material 用的是 sys.exit
        print(str(_e))                          # 它自带「错误：」前缀
        return 1
    if job:
        manifest["step_filter"] = job
    files["README.txt"] = SESSION_README % {
        "mat": manifest["material"], "skill": manifest["skill"],
        "host": manifest["host"] or "-",
        "created": manifest["created"]}
    manifest["files"] = [{"path": k, "bytes": len(v.encode("utf-8")),
                          "sha256": _session_sha(v)}
                         for k, v in sorted(files.items())]
    # manifest.json 自己不做 sha256（自引用无解），但要列出来并说明
    manifest["files"].append({"path": "manifest.json", "self": True,
                              "note": "自引用：manifest 不给自己算 sha256"})
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    if json_out:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    if not write:
        return 0

    # 输出路径：默认 cwd/tmp/（仓库临时文件纪律），没有 tmp/ 就放 cwd
    ts = time.strftime("%Y%m%dT%H%M%S")
    safe = str(manifest["material"]).replace("/", "_")
    if not out:
        base = os.path.join(os.getcwd(), "tmp")
        if not os.path.isdir(base):
            base = os.getcwd()
        out = os.path.join(base, "session_%s_%s.tar.gz" % (safe, ts))
    out = os.path.abspath(os.path.expanduser(out))
    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with tarfile.open(out, "w:gz") as tf_:
            for name in sorted(files):        # 排序 + 固定 mtime → 可复现的包
                data_b = files[name].encode("utf-8")
                ti = tarfile.TarInfo(name)
                ti.size = len(data_b)
                ti.mtime = 0
                ti.mode = 0o644
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = "taskflow"
                tf_.addfile(ti, io.BytesIO(data_b))
    except (OSError, tarfile.TarError) as _e:
        print("错误：写不出包 %s：%s" % (out, _e))
        return 1

    size = os.path.getsize(out)
    c = manifest["counts"]
    print("会话导出  %s（%s，%s）" % (manifest["material"], manifest["skill"],
                                     manifest["host"] or "-"))
    print("  输出      %s（%.1f KB）" % (out, size / 1024.0))
    print("  内含      %d 个文件：%s" % (len(files), ", ".join(sorted(files))))
    print("  数据      历史 %d 条（动作 %d · 完成 %d）· agent 调用 %d（拒绝 %d）"
          " · provenance %d 步"
          % (c["history_events"], c["action_events"], c["finish_events"],
             c["agent_calls"], c["agent_denied"], c["provenance_steps"]))
    kinds = ["%s %s" % (s.get("label"), s.get("kind")) for s in manifest["steps"]]
    if kinds:
        print("  状态      %s" % " · ".join(kinds))
    print("  提示      manifest.json 里逐文件带 sha256；README.txt 讲怎么读/怎么重放。")
    return 0
