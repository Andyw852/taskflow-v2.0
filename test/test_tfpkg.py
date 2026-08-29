# -*- coding: utf-8 -*-
"""taskflow v2.0 单元测试（纯函数 + CLI 冒烟）。

运行：
  cd ~/software/taskflow-v2.0 && python3 test/test_tfpkg.py      # 独立运行器
  cd ~/software/taskflow-v2.0 && python3 -m pytest test/test_tfpkg.py -v   # 若有 pytest
"""
import os
import sys
import ast
import subprocess
import tempfile
import json

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import tfpkg


# ---------- 构造合成 data ----------
def _mk_data():
    return {
        "types": [{
            "key": "opt-mace-cpu",
            "root": "/tmp/root",
            "materials": [
                {"name": "Si", "path": "/tmp/root/Si", "steps": [
                    {"name": "step1", "label": "step1", "kind": "OK"},
                    {"name": "step2", "label": "step2", "kind": "R"},
                ]},
                {"name": "Ge", "path": "/tmp/root/Ge", "steps": [
                    {"name": "step1", "label": "step1", "kind": "FAIL", "diag": "force not converged"},
                    {"name": "step2", "label": "step2", "kind": "OK"},
                ]},
            ],
        }],
        "queue": {"R": 1, "PD": 0, "total": 1},
    }


def test_collector_integrity():
    # COLLECTOR 与原单体字节一致，且独立文件可 py_compile
    src = open(os.path.join(_ROOT, "versions/v1.0/tf"), encoding="utf-8").read()
    tree = ast.parse(src)
    orig = [n.value.value for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "COLLECTOR" for t in n.targets)][0]
    assert tfpkg.COLLECTOR == orig, "COLLECTOR 与原单体不一致"
    remote = open(os.path.join(_ROOT, "tfpkg/_collector_remote.py"), encoding="utf-8").read()
    assert remote == tfpkg.COLLECTOR, "_collector_remote.py 与 COLLECTOR 不一致"
    # 语法可编译
    compile(remote, "_collector_remote.py", "exec")


def test_load_config_file():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("a: x\nb: y\n")
        path = f.name
    try:
        cfg, p = tfpkg.load_config(path)
        assert cfg == {"a": "x", "b": "y"}
        assert p == path
    finally:
        os.unlink(path)


def test_load_config_real():
    cfg, path = tfpkg.load_config(os.path.join(_ROOT, "setting/tf.yaml"))
    assert isinstance(cfg, dict)
    assert "task_types" in cfg
    assert os.path.basename(path) == "tf.yaml"


def test_discover_skills():
    skills = tfpkg.discover_skills({})
    assert len(skills) >= 15, "应发现 15+ 技能，实际 %d" % len(skills)
    for k in ("opt-mace-cpu", "band-dft-cpu", "mlff-mace"):
        assert k in skills, "缺少技能 %s" % k
    assert isinstance(skills["opt-mace-cpu"].get("steps"), list)


def test_apply_skills():
    cfg = {}
    tfpkg.apply_skills(cfg)
    assert "task_types" in cfg
    assert "opt-mace-cpu" in cfg["task_types"]
    assert "_skills" in cfg


def test_merge_type():
    skel = {"a": 1, "b": {"x": 1, "y": 2}}
    over = {"b": {"y": 3}, "c": 4}
    out = tfpkg._merge_type(skel, over)
    assert out["a"] == 1
    assert out["b"] == {"x": 1, "y": 3}, "一层字典应递归合并"
    assert out["c"] == 4


def test_step_status_word():
    assert tfpkg._step_status_word({"kind": "OK"}) == "done"
    assert tfpkg._step_status_word({"kind": "R"}) == "R"
    assert tfpkg._step_status_word({"kind": "FAIL"}) == "FAIL"
    assert tfpkg._step_status_word({"kind": "PD"}) == "PD"
    assert tfpkg._step_status_word({"kind": "PD", "job": {"info": "QOSMaxJobsPerUserLimit"}}) == "PD(QOSMaxJobsPerUserLimit)"
    assert tfpkg._step_status_word({"kind": "WAIT"}) == "wait"
    assert tfpkg._step_status_word({"kind": "SCANCEL"}) == "scancel"
    assert tfpkg._step_status_word({"kind": "PREP"}) == "prep"


def test_summary_lines():
    lines = tfpkg._summary_lines(_mk_data())
    assert any("opt-mace-cpu: 2 材料 done=0 run=1 pd=0 err=1" in l for l in lines)
    assert any("FAIL Ge step1 force not converged" in l for l in lines)
    assert any("队列(全部作业): R=1 PD=0 共 1" in l for l in lines)


def test_summary_json():
    j = tfpkg._summary_json(_mk_data())
    assert j["types"][0]["key"] == "opt-mace-cpu"
    assert j["types"][0]["materials"] == 2
    assert j["types"][0]["counts"] == {"done": 0, "run": 1, "pd": 0, "err": 1, "scancel": 0, "wait": 0}
    assert j["types"][0]["fails"][0]["material"] == "Ge"
    assert j["queue"] == {"R": 1, "PD": 0, "total": 1}


def test_snapshot_diff():
    old = {"t": {"m": {"s1": "todo"}}}
    new = {"t": {"m": {"s1": "PD"}}}
    ch = tfpkg._snapshot_diff(old, new)
    assert ch == [("t", "m", "s1", "todo", "PD")]
    # 无变化 → 空
    assert tfpkg._snapshot_diff(old, old) == []


def test_natkey():
    assert sorted(["S10", "S2", "S1"], key=tfpkg._natkey) == ["S1", "S2", "S10"]


def _run(cmd):
    p = subprocess.run(cmd, shell=True, cwd=_ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def test_cli_smoke():
    tf = "python3 bin/tf"
    for cmd, name in [
        (tf + " --help", "--help"),
        (tf + " --schema", "--schema"),
        (tf + " config", "config"),
        (tf + " skills", "skills"),
        (tf + " --version", "--version"),
    ]:
        rc, out, err = _run(cmd)
        assert rc == 0, "%s 失败 rc=%d err=%s" % (name, rc, err)
    # json schema 输出含 version 标记
    rc, out, err = _run(tf + " --schema")
    assert "schema_version 2" in out


def test_cli_json_flag():
    # 空配置 + list/summary --json 应输出 JSON
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("{}\n")
        path = f.name
    try:
        rc, out, err = _run("python3 bin/tf -c %s list --json" % path)
        assert rc == 0, "rc=%d err=%s" % (rc, err)
        d = json.loads(out)
        assert "types" in d
        rc, out, err = _run("python3 bin/tf -c %s summary --json" % path)
        assert rc == 0 and json.loads(out)["types"] == []
    finally:
        os.unlink(path)


def test_cli_dry_run():
    # --dry-run 不执行变更，只打印对象
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("{}\n")
        path = f.name
    try:
        rc, out, err = _run("python3 bin/tf -c %s start --dry-run" % path)
        assert rc == 0, "rc=%d err=%s" % (rc, err)
        assert "【dry-run】" in out
        assert "0 个材料" in out
    finally:
        os.unlink(path)


def test_cli_diagnose():
    # 沙盒：一键结构化诊断（只读、不提交）
    sand = os.path.join(_ROOT, "test", "sandbox", "tf.yaml")
    rc, out, err = _run("python3 bin/tf -c %s -tt opt-mace-cpu -p Si diagnose" % sand)
    assert rc == 0, "rc=%d err=%s" % (rc, err)
    d = json.loads(out)
    assert d["material"] == "Si"
    assert d["type"] == "opt-mace-cpu"
    assert "steps" in d and "hpc" in d


def test_cli_json_filters():
    # 沙盒：json --errors-only / --limit 分页
    sand = os.path.join(_ROOT, "test", "sandbox", "tf.yaml")
    rc, out, err = _run("python3 bin/tf -c %s json --errors-only" % sand)
    assert rc == 0, "rc=%d err=%s" % (rc, err)
    assert "types" in json.loads(out)
    rc, out, err = _run("python3 bin/tf -c %s json --limit 1" % sand)
    assert rc == 0, "rc=%d err=%s" % (rc, err)
    d = json.loads(out)
    assert "materials" in d and "total" in d
    assert len(d["materials"]) <= 1


def test_cli_json_changes():
    # 沙盒：json --changes 输出结构化变更快照
    sand = os.path.join(_ROOT, "test", "sandbox", "tf.yaml")
    rc, out, err = _run("python3 bin/tf -c %s json --changes" % sand)
    assert rc == 0, "rc=%d err=%s" % (rc, err)
    d = json.loads(out)
    assert "changes" in d and "count" in d and "first_run" in d
    assert d["schema_version"] == 2


def test_stepconf_reserved_params():
    # 集群注入键（POTCAR_DIR/REFERENCES_DIR 等）必须在白名单里，
    # 否则 MACE gen 脚本会误报"不认识的键"（v2.0 拷贝回归的回归测试）。
    import importlib.util
    sp = importlib.util.spec_from_file_location(
        "_stepconf", os.path.join(_ROOT, "skill/_common/opt/stepconf.py"))
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    for k in ("POTCAR_DIR", "REFERENCES_DIR", "MACE_MODEL_DIR", "AMSET_ENV",
              "CONDA_SH", "CONDA_ENV", "BANDGAP"):
        assert k in mod.RESERVED_PARAMS, "%s 缺失" % k
    # StepConf 对保留键应无条件放行（spec 里没有也不报错）
    merged = mod.parse("[params]\nFMAX = 0.01\nPOTCAR_DIR = /x\nREFERENCES_DIR = /y\n")
    sc = mod.StepConf(merged, {"FMAX": (0.01, "float")}, path="t")
    assert sc["FMAX"] == 0.01


def test_retry_targets():
    m = {"steps": [
        {"name": "s1", "label": "s1", "kind": "FAIL"},
        {"name": "s2", "label": "s2", "kind": "OK"},
        {"name": "s3", "label": "s3", "kind": "R"},
    ]}
    retryable = lambda s: s["kind"] == "FAIL"
    tgt = tfpkg._retry_targets(m, retryable)
    assert [s["name"] for s in tgt] == ["s1"], "retry 应只命中 FAIL 步"


def test_dry_run_steps_for():
    # --dry-run 按命令语义筛真实目标：retry=FAIL，start=就绪，stop=有作业
    s1 = {"name": "s1", "label": "S1", "kind": "FAIL"}
    s2 = {"name": "s2", "label": "S2", "kind": "TODO"}
    s3 = {"name": "s3", "label": "S3", "kind": "R", "job": {"id": "9"}}
    m = {"steps": [s1, s2, s3], "actives": [s2]}
    assert [s["name"] for s in tfpkg._dry_run_steps_for("retry", m, None)] == ["s1"]
    assert [s["name"] for s in tfpkg._dry_run_steps_for("start", m, None)] == ["s2"]
    assert [s["name"] for s in tfpkg._dry_run_steps_for("stop", m, None)] == ["s3"]
    assert [s["name"] for s in tfpkg._dry_run_steps_for("fetch", m, None)] == []
    # 带 -j 指定步骤时按名字命中（即使非 FAIL）
    assert [s["name"] for s in tfpkg._dry_run_steps_for("retry", m, "S2")] == ["s2"]
    # rerun/clean 无 -j 是整材料级
    assert tfpkg._dry_run_steps_for("rerun", m, None) is None


def test_diag_code():
    # 诊断文本 → 稳定结构化错误码（--json 机器判读）
    assert tfpkg._diag_code("relax_summary.json missing") == "relax_summary_missing"
    assert tfpkg._diag_code("relax_summary.json incomplete") == "relax_summary_incomplete"
    assert tfpkg._diag_code("force not converged") == "force_not_converged"
    assert tfpkg._diag_code("未收敛 [oscillating] 大幅振荡") == "relax_oscillating"
    assert tfpkg._diag_code("job RUNNING") == "job"
    assert tfpkg._diag_code("") == "none"
    assert tfpkg._diag_code("随便什么未知错误") == "unknown"
    # _summary_json 的 fails 带 code 字段
    j = tfpkg._summary_json(_mk_data())
    assert j["types"][0]["fails"][0]["code"] == "force_not_converged"
    # fails 现在带建议动作（机器化 AGENTS.md §5 决策表）
    assert j["types"][0]["fails"][0]["action"] == "retry"


def test_diag_action_map():
    # diag_code → 确定性建议动作（机器化决策表）
    assert tfpkg._suggested_action("force_not_converged")[0] == "retry"
    assert tfpkg._suggested_action("relax_nsw")[0] == "retry"
    assert tfpkg._suggested_action("node_fail")[0] == "retry"
    assert tfpkg._suggested_action("dir_missing")[0] == "rerun"
    assert tfpkg._suggested_action("not_started")[0] == "start"
    assert tfpkg._suggested_action("stepconf_unknown_params")[0] == "human_review"
    assert tfpkg._suggested_action("unknown")[0] == "human_review"
    assert tfpkg._suggested_action("none") == ("none", "")
    # 每个 retry/rerun 动作都有理由
    for code, (act, reason) in tfpkg._ACTION_MAP.items():
        assert reason, "%s 缺理由" % code


def test_diagnose():
    # cmd_diagnose：默认输出 FAIL 步，结构化带 diag_code + suggested_action
    d = tfpkg.cmd_diagnose({}, _mk_data(), "Ge", None)
    assert d["material"] == "Ge"
    assert d["type"] == "opt-mace-cpu"
    assert len(d["steps"]) == 1
    s = d["steps"][0]
    assert s["label"] == "step1"
    assert s["kind"] == "FAIL"
    assert s["diag_code"] == "force_not_converged"
    assert s["suggested_action"] == "retry"
    assert s["action_reason"]
    # -j 指定非 FAIL 步也输出
    d2 = tfpkg.cmd_diagnose({}, _mk_data(), "Ge", "step2")
    assert len(d2["steps"]) == 1 and d2["steps"][0]["kind"] == "OK"


def test_json_errors_only():
    # 只保留含 FAIL 步骤的材料，且只留 FAIL 步骤
    out = tfpkg._json_errors_only(_mk_data())
    mats = out["types"][0]["materials"]
    assert [m["name"] for m in mats] == ["Ge"]          # Si 无 FAIL 被滤掉
    assert [s["label"] for s in mats[0]["steps"]] == ["step1"]  # 只留 FAIL 步


def test_json_paginate():
    # 展平材料分页：扁平结构 + total/offset/limit
    out = tfpkg._json_paginate(_mk_data(), 0, 10)
    assert out["total"] == 2
    assert out["offset"] == 0 and out["limit"] == 10
    names = [m["name"] for m in out["materials"]]
    assert names == ["Ge", "Si"]                        # 按 (type,name) 排序
    assert out["materials"][0]["type"] == "opt-mace-cpu"
    # limit=1 只取 1 个
    assert len(tfpkg._json_paginate(_mk_data(), 0, 1)["materials"]) == 1
    # offset=1 跳过第 1 个
    assert tfpkg._json_paginate(_mk_data(), 1, 10)["materials"][0]["name"] == "Si"


def test_json_changes():
    # 首次无基线 → first_run；改数据 → 出变更；再跑同数据 → unchanged
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        r1 = tfpkg._json_changes(_mk_data(), path)
        assert r1["first_run"] is True and r1["count"] == 0
        # Ge step1 FAIL → OK，状态词 FAIL → done
        d2 = _mk_data()
        d2["types"][0]["materials"][1]["steps"][0]["kind"] = "OK"
        r2 = tfpkg._json_changes(d2, path)
        assert r2["first_run"] is False and r2["count"] == 1
        c = r2["changes"][0]
        assert (c["type"], c["material"], c["step"]) == ("opt-mace-cpu", "Ge", "step1")
        assert c["old"] == "FAIL" and c["new"] == "done"
        # 同数据再跑 → unchanged
        r3 = tfpkg._json_changes(d2, path)
        assert r3["unchanged"] is True and r3["count"] == 0
    finally:
        os.remove(path)


def test_yamlmini_module():
    # yamlmini 是真深模块：可独立 import，parse() 是唯一对外接口
    import tfpkg.yamlmini as y
    assert y.parse("a: 1\nb:\n  - 2\n  - 3") == {"a": 1, "b": [2, 3]}
    # 装配后注入共享命名空间，行为不变（_mini_yaml 同源）
    assert tfpkg._mini_yaml is y._mini_yaml
    assert tfpkg.parse is y.parse


def test_data_module():
    # data 是真深模块：collect_data + 过滤 + 缓存 + 快照，环2 破
    import tfpkg.data as d
    assert callable(d.collect_data) and callable(d.filter_status)
    assert tfpkg.collect_data is d.collect_data
    # filter_status 空数据/空 spec 不报错、返回 None
    assert d.filter_status({"types": []}, "error") is None
    # 延迟 import 生效：collect_data 函数体里能取到包命名空间的 collect/annotate
    src = __import__("inspect").getsource(d.collect_data)
    assert "from tfpkg import" in src


def test_workflow_module():
    # workflow 是大深模块：06/09/13/11 合并，环1 消除
    import tfpkg.workflow as w
    for name in ("step_state", "do_submit", "auto_advance", "auto_fetch",
                 "remote_gen", "cmd_start", "cmd_stop", "cmd_retry",
                 "cmd_rerun", "cmd_clean", "annotate", "check_duplicates",
                 "do_rerun_step", "kill_if_queued"):
        assert callable(getattr(w, name, None)), name
    # 注入共享命名空间，行为不变
    assert tfpkg.step_state is w.step_state
    assert tfpkg.do_submit is w.do_submit


def test_namespace_complete():
    # 深模块化安全网：8 个真模块的名字全部注入包命名空间
    import tfpkg
    stdlib = {"os", "sys", "re", "json", "time", "shlex", "hashlib", "base64",
              "collections", "functools", "itertools", "subprocess", "tempfile",
              "threading", "socket", "argparse", "glob", "math", "random", "shutil",
              "Counter", "defaultdict", "ThreadPoolExecutor", "datetime", "copy",
              "pathlib", "getpass", "textwrap", "urllib", "io", "string", "signal",
              "ast", "inspect", "warnings", "csv"}
    for modname in ("bootstrap", "collect", "data", "workflow", "report",
                    "ops", "cli", "yamlmini"):
        m = getattr(tfpkg, modname)
        missing = [n for n in vars(m)
                   if not n.startswith("__") and n not in stdlib
                   and not hasattr(tfpkg, n)]
        assert not missing, "%s 未注入: %s" % (modname, missing)


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
            passed += 1
        except Exception as e:
            print("FAIL  " + name + "  ->  " + repr(e))
            failed += 1
    print("\n%d passed, %d failed, %d total" % (passed, failed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

