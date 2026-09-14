"""Offline fetch lifecycle regressions: real tar, no SSH or scheduler jobs."""
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tfpkg
from tfpkg import workflow


def engine(version):
    if version == "v2":
        return workflow, tfpkg
    path = ROOT.parent / "taskflow/versions/v1.0/tf"
    loader = importlib.machinery.SourceFileLoader("tf_fetch_v1", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module, module


def material(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    for name in ("INCAR", "POSCAR", "KPOINTS", "submit.sh"):
        (remote / name).write_text("input\n")
    (remote / "forces.npy").write_bytes(b"large raw array")
    step = dict(name="step1", label="S1", dir=str(remote), exists=True,
                done=False, has_incar=True, submit="submit.sh")
    m = dict(name="test", tt="test-dft-cpu", result_dir=str(tmp_path / "result"),
             fetch_files=["INCAR", "POSCAR", "KPOINTS", "submit.sh", "OUTCAR", "forces.npy"],
             steps=[step])
    return m, step, remote, tmp_path / "result/step1"


def input_fetch_then_completion_fetches_output_once(engine, material):
    api, namespace = engine
    m, s, remote, dest = material
    data = {"types": [{"materials": [m]}]}
    with patch.object(namespace, "log_action"):
        if api is workflow:
            assert api._remote_submit_preflight({}, m, s) == (True, "")
        else:
            assert api.fetch_material({}, m, quiet=True)
        assert (dest / "INCAR").is_file()
        if api is workflow:
            assert not (dest / "forces.npy").exists()
        else:
            # v1 has no remote-submit preflight; its direct fetch API intentionally
            # uses the full manifest and is tested by the completion lifecycle below.
            assert (dest / "forces.npy").is_file()
        (remote / "OUTCAR").write_text("completed output\n")
        s["done"] = True
        api.auto_fetch({}, data)
        assert (dest / "OUTCAR").read_text() == "completed output\n"
        with patch.object(api, "fetch_material") as transfer:
            api.auto_fetch({}, data)
            transfer.assert_not_called()


class FetchLifecycleTests(unittest.TestCase):
    def test_receipt_lifecycle(self):
        for version in ("v2", "v1"):
            for scenario in ("legacy", "nonempty", "running", "rerun", "failure", "scope", "virtual", "regenerate", "legacy_expanded", "all_empty"):
                with self.subTest(version=version, scenario=scenario):
                    with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as folder:
                        api, namespace = engine(version)
                        m, s, remote, dest = material(Path(folder))
                        data = {"types": [{"materials": [m]}]}
                        dest.mkdir(parents=True)
                        (dest / "INCAR").write_text("input only")
                        if scenario in ("legacy", "legacy_expanded"):
                            (dest / ".tf_fetched").touch()
                        if scenario == "legacy_expanded":
                            m["fetch_files"].append("forces.npy")
                            (remote / "forces.npy").write_bytes(b"raw forces")
                        s["done"] = True
                        (remote / "OUTCAR").write_text("first")
                        with patch.object(namespace, "log_action"):
                            if scenario == "virtual":
                                s["exists"] = False
                            if scenario == "all_empty":
                                m["fetch_files"] = []
                                self.assertTrue(api.fetch_material({}, m, all_files=True, quiet=True))
                            api.auto_fetch({}, data)
                            if scenario == "legacy_expanded":
                                self.assertEqual((dest / "forces.npy").read_bytes(), b"raw forces")
                            if scenario != "virtual":
                                self.assertEqual((dest / "OUTCAR").read_text(), "first")
                            if scenario in ("running", "rerun", "failure", "scope", "regenerate"):
                                if scenario == "running":
                                    s["job"] = {"id": "12", "state": "R"}
                                    # Even inconsistent done=True must not stamp a live job.
                                    api.fetch_material({}, m, quiet=True)
                                    self.assertFalse((dest / ".tf_fetched").exists())
                                    s.pop("job")
                                elif scenario == "regenerate":
                                    with patch.object(api, "remote_gen", return_value=(True, "generated")), patch.object(namespace, "step_cfg", return_value={}):
                                        self.assertTrue(api.do_submit({}, {}, m, s, False, True, False, "test", submit=False))
                                    self.assertFalse((dest / ".tf_fetched").exists())
                                    self.assertFalse(s["done"])
                                    with patch.object(api, "fetch_material") as transfer:
                                        api.auto_fetch({}, data)
                                        transfer.assert_not_called()
                                elif scenario == "rerun":
                                    with patch.object(api, "remote_sbatch", return_value=(True, "", "42")), patch.object(namespace, "step_cfg", return_value={}):
                                        self.assertTrue(api.do_submit({}, {}, m, s, False, False, False, "test"))
                                    self.assertFalse(s["done"])
                                    self.assertFalse((dest / ".tf_fetched").exists())
                                    with patch.object(api, "fetch_material") as transfer:
                                        api.auto_fetch({}, data)
                                        transfer.assert_not_called()
                                elif scenario == "failure":
                                    old_dir = s["dir"]
                                    s["dir"] = str(remote / "missing")
                                    self.assertFalse(api.fetch_material({}, m, quiet=True))
                                    self.assertFalse((dest / ".tf_fetched").exists())
                                    s["dir"] = old_dir
                                else:
                                    m["fetch_files"].append("new.txt")
                                    (remote / "new.txt").write_text("new scope")
                                (remote / "OUTCAR").write_text("second")
                                s["done"] = True  # next collection sees the new run complete
                                api.auto_fetch({}, data)
                                self.assertEqual((dest / "OUTCAR").read_text(), "second")
                            with patch.object(api, "fetch_material") as transfer:
                                api.auto_fetch({}, data)
                                transfer.assert_not_called()


    def test_input_fetch_then_completion(self):
        for version in ("v2", "v1"):
            with self.subTest(version=version):
                with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as folder:
                    input_fetch_then_completion_fetches_output_once(
                        engine(version), material(Path(folder)))


if __name__ == "__main__":
    unittest.main()
