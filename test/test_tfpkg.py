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

