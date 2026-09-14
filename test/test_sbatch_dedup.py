# -*- coding: utf-8 -*-
"""taskflow v2.0 重复提交去重回归测试（mocked scheduler 并发）。"""
import os, sys, json, time, tempfile, threading, subprocess
import unittest.mock as _mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
import tfpkg
from tfpkg import workflow

_FAKE_SCHED = "import os, sys, json, time, re, fcntl\nSTATE = os.environ.get('TF_FAKE_STATE')\nLOCK = STATE + '.lock'\ndef _fenv(k, dflt):\n    try: return float(os.environ.get(k, str(dflt)))\n    except ValueError: return dflt\ndef _load_unlocked():\n    if os.path.isfile(STATE): return json.load(open(STATE))\n    return {'next_id': 1000, 'sbatch_count': 0, 'jobs': []}\ndef _read():\n    with open(LOCK, 'a+') as lf:\n        fcntl.flock(lf, fcntl.LOCK_EX)\n        try: return _load_unlocked()\n        finally: fcntl.flock(lf, fcntl.LOCK_UN)\ndef _mutate(fn):\n    with open(LOCK, 'a+') as lf:\n        fcntl.flock(lf, fcntl.LOCK_EX)\n        try:\n            d = _load_unlocked(); r = fn(d)\n            tmp = STATE + '.tmp'\n            json.dump(d, open(tmp, 'w')); os.replace(tmp, STATE)\n            return r\n        finally:\n            fcntl.flock(lf, fcntl.LOCK_UN)\ndef cmd_sbatch(args):\n    script = args[-1] if args else ''\n    name = ''\n    try:\n        m = re.search(r'^#SBATCH\\s+--job-name=(\\S+)', open(script).read(), re.M)\n        name = m.group(1) if m else ''\n    except OSError: pass\n    holder = {}\n    def go(d):\n        jid = d['next_id']; d['next_id'] += 1\n        d['sbatch_count'] += 1\n        d['jobs'].append({'id': str(jid), 'name': name, 'wd': os.getcwd(), 'state': 'PENDING', 'ts': time.time()})\n        holder['jid'] = jid\n    _mutate(go)\n    print('Submitted batch job %d' % holder['jid'])\ndef cmd_squeue(args):\n    if os.environ.get('TF_FAKE_SQUEUE_FAIL') == '1':\n        sys.stderr.write('slurm_load_jobs error: Invalid partition\\n'); sys.exit(1)\n    fmt = ''\n    for i, a in enumerate(args):\n        if a == '-o' and i + 1 < len(args): fmt = args[i + 1]\n    delay = _fenv('TF_FAKE_SQUEUE_DELAY', 0)\n    dur = _fenv('TF_FAKE_JOB_DURATION', 999999)\n    now = time.time(); d = _read()\n    use_wd = '%Z' in fmt\n    only_name = ('%i' not in fmt) and ('%j' in fmt)\n    for j in d['jobs']:\n        age = now - j['ts']\n        if age < delay: continue\n        if age >= dur: continue\n        if only_name: print(j['name'])\n        elif use_wd: print('%s|%s|%s' % (j['id'], j['wd'], j['state']))\n        else: print('%s|%s|%s' % (j['id'], j['name'], j['state']))\n    sys.exit(0)\ndef cmd_sacct(args):\n    if os.environ.get('TF_FAKE_SACCT_FAIL') == '1':\n        sys.stderr.write('sacct: error: slurmdbd unreachable\\n'); sys.exit(1)\n    jid = None\n    for i, a in enumerate(args):\n        if a == '-j' and i + 1 < len(args): jid = args[i + 1]\n    delay = _fenv('TF_FAKE_SACCT_DELAY', 0)\n    dur = _fenv('TF_FAKE_JOB_DURATION', 999999)\n    now = time.time(); d = _read()\n    for j in d['jobs']:\n        if j['id'] == jid:\n            age = now - j['ts']\n            if age < delay: sys.exit(0)\n            print('%s|%s|' % (j['id'], 'RUNNING' if age < dur else 'COMPLETED')); sys.exit(0)\n    sys.exit(0)\nif __name__ == '__main__':\n    cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n    a = sys.argv[2:]\n    if cmd == 'sbatch': cmd_sbatch(a)\n    elif cmd == 'squeue': cmd_squeue(a)\n    elif cmd == 'sacct': cmd_sacct(a)\n    else: sys.exit(2)\n"

def _make_scheduler(tmp):
    bin_dir = os.path.join(tmp, "fakebin")
    os.makedirs(bin_dir, exist_ok=True)
    with open(os.path.join(bin_dir, "fake_sched.py"), "w") as f:
        f.write(_FAKE_SCHED)
    for name in ("sbatch", "squeue", "sacct"):
        p = os.path.join(bin_dir, name)
        with open(p, "w") as f:
            f.write('#!/usr/bin/env bash\nexec python3 "$(dirname "$0")/fake_sched.py" %s "$@"\n' % name)
        os.chmod(p, 0o755)
    return bin_dir, os.path.join(tmp, "state.json")


def _make_mock(bin_dir, state, delays):
    def fake_run_remote(cfg, shell_line, host="__default__", use_stdin=False):
        env = dict(os.environ)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["TF_FAKE_STATE"] = state
        for k, v in delays.items():
            env[k] = str(v)
        r = subprocess.run(["bash", "-c", shell_line], env=env,
                           capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).strip()
    return fake_run_remote


def _read_state(state):
    if not os.path.isfile(state):
        return {"next_id": 1000, "sbatch_count": 0, "jobs": []}
    with open(state) as f:
        return json.load(f)


def _mk_step(tmp, fanout=False, subdirs=("d1", "d2", "d3")):
    step_dir = os.path.join(tmp, "Mg4C60", "S6_elastic")
    os.makedirs(step_dir, exist_ok=True)
    with open(os.path.join(step_dir, "submit.sh"), "w") as f:
        f.write("#!/bin/bash\n#SBATCH --job-name=x\n#SBATCH --ntasks=1\necho run\n")
    s = {"dir": step_dir, "submit": "submit.sh", "_host": "fakehost"}
    if fanout:
        s["fanout"] = "d*"
        for d in subdirs:
            p = os.path.join(step_dir, d)
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, "submit.sh"), "w") as f:
                f.write("#!/bin/bash\n#SBATCH --job-name=x\n#SBATCH --ntasks=1\necho run\n")
    return s, step_dir


def _concurrent(fn, n=2):
    barrier = threading.Barrier(n)
    results = [None] * n
    def worker(i):
        barrier.wait()
        results[i] = fn()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join()
    return results


def test_remote_sbatch_dedup_concurrent_squeue_visible():
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        s, step_dir = _mk_step(tmp)
        mock = _make_mock(bin_dir, state, {"TF_FAKE_SQUEUE_DELAY": 0, "TF_FAKE_JOB_DURATION": 999999})
        with _mock.patch.object(tfpkg, "run_remote", side_effect=mock):
            results = _concurrent(lambda: workflow.remote_sbatch({}, s, jobname="Mg4C60-test-S6"))
        assert sum(1 for r in results if r[0]) == 1, "应只有一路提交成功: %r" % (results,)
        assert _read_state(state)["sbatch_count"] == 1


def test_remote_sbatch_dedup_visibility_delay_receipt_sacct():
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        s, step_dir = _mk_step(tmp)
        mock = _make_mock(bin_dir, state, {"TF_FAKE_SQUEUE_DELAY": 10, "TF_FAKE_SACCT_DELAY": 0, "TF_FAKE_JOB_DURATION": 999999})
        with _mock.patch.object(tfpkg, "run_remote", side_effect=mock):
            results = _concurrent(lambda: workflow.remote_sbatch({}, s, jobname="Mg4C60-test-S6"))
        assert sum(1 for r in results if r[0]) == 1, "应只有一路提交成功: %r" % (results,)
        assert _read_state(state)["sbatch_count"] == 1
        ref = [r for r in results if not r[0]]
        assert ref and ("回执" in (ref[0][1] or "") or "sacct" in (ref[0][1] or "")), "拒绝理由应含回执/sacct: %r" % (ref,)


def test_remote_sbatch_fanout_dedup_concurrent():
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        s, step_dir = _mk_step(tmp, fanout=True, subdirs=("d1", "d2", "d3"))
        mock = _make_mock(bin_dir, state, {"TF_FAKE_SQUEUE_DELAY": 0, "TF_FAKE_JOB_DURATION": 999999})
        with _mock.patch.object(tfpkg, "run_remote", side_effect=mock):
            results = _concurrent(lambda: workflow.remote_sbatch({}, s, jobname="Mg4C60-test-S6"))
        assert sum(1 for r in results if r[0]) == 1, "应只有一路提交成功: %r" % (results,)
        assert _read_state(state)["sbatch_count"] == 3


def test_remote_sbatch_fail_closed_on_squeue_failure():
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        s, step_dir = _mk_step(tmp)
        mock = _make_mock(bin_dir, state, {"TF_FAKE_SQUEUE_FAIL": 1})
        with _mock.patch.object(tfpkg, "run_remote", side_effect=mock):
            ok, out, jid = workflow.remote_sbatch({}, s, jobname="Mg4C60-test-S6")
        assert ok is False
        assert "squeue" in (out or "")
        assert _read_state(state)["sbatch_count"] == 0


def test_remote_sbatch_single_submit_ok():
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        s, step_dir = _mk_step(tmp)
        mock = _make_mock(bin_dir, state, {"TF_FAKE_SQUEUE_DELAY": 0, "TF_FAKE_JOB_DURATION": 999999})
        with _mock.patch.object(tfpkg, "run_remote", side_effect=mock):
            ok, out, jid = workflow.remote_sbatch({}, s, jobname="Mg4C60-test-S6")
        assert ok is True and jid
        assert _read_state(state)["sbatch_count"] == 1
        assert os.path.isfile(os.path.join(step_dir, ".tf_job_receipt"))


def test_remote_sbatch_resubmit_after_completion():
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        s, step_dir = _mk_step(tmp)
        mock = _make_mock(bin_dir, state, {"TF_FAKE_SQUEUE_DELAY": 0, "TF_FAKE_SACCT_DELAY": 0, "TF_FAKE_JOB_DURATION": 0.2})
        with _mock.patch.object(tfpkg, "run_remote", side_effect=mock):
            ok1, _, jid1 = workflow.remote_sbatch({}, s, jobname="Mg4C60-test-S6")
            assert ok1 and jid1
            time.sleep(0.5)
            ok2, _, jid2 = workflow.remote_sbatch({}, s, jobname="Mg4C60-test-S6")
        assert ok2 and jid2 != jid1
        assert _read_state(state)["sbatch_count"] == 2



def test_defects_common_guarded_sbatch_dedup():
    """defect-dft-cpu step0 参考相守卫：两路并发 guarded_sbatch 同一目录 → 只有 1 个 sbatch。"""
    sys.path.insert(0, os.path.join(_ROOT, "skill", "defect-dft-cpu"))
    import defects_common as D
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        workdir = os.path.join(tmp, "refs", "Pb_fcc")
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "submit.sh"), "w") as f:
            f.write("#!/bin/bash\n#SBATCH --job-name=ref_Pb_fcc\necho run\n")
        old_path, old_state = os.environ.get("PATH"), os.environ.get("TF_FAKE_STATE")
        os.environ["PATH"] = bin_dir + os.pathsep + (old_path or "")
        os.environ["TF_FAKE_STATE"] = state
        os.environ["TF_FAKE_SQUEUE_DELAY"] = "0"
        os.environ["TF_FAKE_JOB_DURATION"] = "999999"
        try:
            results = _concurrent(lambda: D.guarded_sbatch(workdir, "ref_Pb_fcc")[0])
        finally:
            os.environ["PATH"] = old_path
            if old_state is not None:
                os.environ["TF_FAKE_STATE"] = old_state
            else:
                os.environ.pop("TF_FAKE_STATE", None)
        assert _read_state(state)["sbatch_count"] == 1
        assert all(r for r in results), "应都 ok（一个提交，一个幂等跳过）: %r" % (results,)


def test_defects_common_guarded_sbatch_fail_closed():
    """defect-dft-cpu 参考相守卫：squeue 查询失败 → fail closed（拒绝 sbatch）。"""
    sys.path.insert(0, os.path.join(_ROOT, "skill", "defect-dft-cpu"))
    import defects_common as D
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        workdir = os.path.join(tmp, "refs", "Pb_fcc")
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "submit.sh"), "w") as f:
            f.write("#!/bin/bash\necho run\n")
        old_path, old_state = os.environ.get("PATH"), os.environ.get("TF_FAKE_STATE")
        os.environ["PATH"] = bin_dir + os.pathsep + (old_path or "")
        os.environ["TF_FAKE_STATE"] = state
        os.environ["TF_FAKE_SQUEUE_FAIL"] = "1"
        try:
            ok, msg = D.guarded_sbatch(workdir, "ref_Pb_fcc")
        finally:
            os.environ["PATH"] = old_path
            if old_state is not None:
                os.environ["TF_FAKE_STATE"] = old_state
            else:
                os.environ.pop("TF_FAKE_STATE", None)
            os.environ.pop("TF_FAKE_SQUEUE_FAIL", None)
        assert ok is False
        assert "squeue" in msg
        assert _read_state(state)["sbatch_count"] == 0


def test_remote_sbatch_fanout_retry_only_unfinished():
    """fanout retry 补帧（fan_todo）：只提交未完成子目录，已完成子目录不重复提交。"""
    with tempfile.TemporaryDirectory(dir=os.path.join(_ROOT, "tmp")) as tmp:
        bin_dir, state = _make_scheduler(tmp)
        s, step_dir = _mk_step(tmp, fanout=True, subdirs=("d1", "d2", "d3"))
        s["fan_todo"] = ["d2"]   # retry 只补 d2（d1/d3 已完成）
        mock = _make_mock(bin_dir, state, {"TF_FAKE_SQUEUE_DELAY": 0, "TF_FAKE_JOB_DURATION": 999999})
        with _mock.patch.object(tfpkg, "run_remote", side_effect=mock):
            ok, out, jid = workflow.remote_sbatch({}, s, jobname="Mg4C60-test-S6")
        assert ok is True, "fanout retry 应提交成功: %r" % (out,)
        st = _read_state(state)
        assert st["sbatch_count"] == 1, "fan_todo=[d2] 应只提交 1 个子目录: %r" % (st,)
        assert len(st["jobs"]) == 1
        assert st["jobs"][0]["wd"].rstrip("/").endswith("d2"), "应提交 d2 而非其它子目录: %r" % (st["jobs"],)


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
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