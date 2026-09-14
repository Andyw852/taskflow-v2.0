"""Regression checks for public guard boundary cases; local fake scheduler only."""
import os
import runpy
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
t = runpy.run_path(str(ROOT / "test/test_sbatch_dedup.py"))

def test_fanout_vs_direct_child():
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
        bin_dir, state = t["_make_scheduler"](tmp)
        parent, directory = t["_mk_step"](tmp, fanout=True, subdirs=("d1",))
        child = dict(parent, dir=os.path.join(directory, "d1"))
        child.pop("fanout")
        mock = t["_make_mock"](bin_dir, state, {"TF_FAKE_SQUEUE_DELAY": 20})
        import threading
        counter = iter((parent, child))
        mutex = threading.Lock()
        def submit():
            with mutex:
                step = next(counter)
            return t["workflow"].remote_sbatch({}, step, jobname="boundary")
        with t["_mock"].patch.object(t["tfpkg"], "run_remote", side_effect=mock):
            results = t["_concurrent"](submit)
        assert t["_read_state"](state)["sbatch_count"] == 1, results

def test_aged_unknown_receipt_blocks():
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
        bin_dir, state = t["_make_scheduler"](tmp)
        step, directory = t["_mk_step"](tmp)
        Path(directory, ".tf_job_receipt").write_text("98765 1\n")
        mock = t["_make_mock"](bin_dir, state, {})
        with t["_mock"].patch.object(t["tfpkg"], "run_remote", side_effect=mock):
            result = t["workflow"].remote_sbatch({}, step, jobname="boundary")
        assert not result[0], result

if __name__ == "__main__":
    failed = 0
    for name, function in sorted(list(globals().items())):
        if name.startswith("test_"):
            try:
                function()
                print("PASS", name)
            except Exception as error:
                failed += 1
                print("FAIL", name, repr(error))
    raise SystemExit(bool(failed))
