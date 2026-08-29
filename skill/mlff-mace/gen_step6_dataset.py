#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step6_dataset.py —— mlff-mace step6：OUTCAR → extxyz 数据集（run: gen）。

运行位置：超算登录节点，cwd = 材料目录。重活由 dataset_build.py 在 venv 里干。
训练集/测试集跨代累积、永不丢弃（引擎每次遍历 step5 全部已算完帧 + PRE_XYZ_FILES）。

读：step5_label/cfg-*/OUTCAR + step4 各代清单 + step.conf（DATA_MODE/PRE_XYZ_FILES/
    过滤阈值）+ step1 的 INCAR/POTCAR（指纹）
写：step6_dataset/gen-<K>/{train.xyz, test.xyz, all.xyz, e0s.json, dataset_summary.json,
    coverage_report.json, energy_forces.png} + 顶层 dataset_summary.json 副本
退出码 0 成功；非 0 = [ERROR]（含「S5 还没算完本代帧」——等 S5 OK 后 retry 本步）。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc
import stepconf

OUTDIR = "step6_dataset"
STEP = "step6_dataset"


def outcar_done(path):
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with open(p, "rb") as fh:
            try:
                fh.seek(-200000, 2)
            except OSError:
                fh.seek(0)
            return "General timing and accounting" in \
                fh.read().decode("utf-8", "ignore")
    except OSError:
        return False


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)
    cv = dict(conf.params)

    man_path = cwd / "step4_genstruct" / "struct_manifest.json"
    if not man_path.is_file():
        sys.exit("[ERROR] step4 清单不存在 —— 先让 S4 生成结构。")
    man = json.loads(man_path.read_text())
    gen = man["generation"]

    # ---- 本代帧必须全部算完才建数据集（不许拿半代数据训练）----
    missing = [e["id"] for e in man["frames"]
               if not outcar_done(cwd / "step5_label" / e["id"] / "OUTCAR")]
    if missing:
        sys.exit("[ERROR] 第 %d 代还有 %d 帧没算完（%s ...）。\n"
                 "        等 S5_label 全部 OK 后再 retry 本步（tf -p <材料> -j 6 retry）。"
                 % (gen, len(missing), ", ".join(missing[:6])))

    method = mc.read_kv(cwd / "step1_relax" / mc.METHOD_FILE)
    dim = (method.get("DIM") or "3d").lower()
    if dim == "0d":
        sys.exit("[ERROR] 0D 体系不支持。")

    rc = mc.run_py("dataset_build.py",
                   "--gen %d --outdir %s/gen-%d --dim %s "
                   "--energy-limit %g --force-limit %g --kspacing-tol %g "
                   "--pre-xyz '%s' --fps-seed 42 "
                   "--vol-factors '%s' --n-per-cell %d"
                   % (gen, OUTDIR, gen, dim, cv["ENERGY_LIMIT"], cv["FORCE_LIMIT"],
                      cv["KSPACING_TOL"], cv["PRE_XYZ_FILES"] or "",
                      cv["VOL_FACTORS"], cv["N_PER_CELL"]),
                   cwd, conf=cv, logname="dataset.log")
    if rc != 0:
        sys.exit("[ERROR] dataset_build.py 失败（rc=%d），看 dataset.log 尾部。" % rc)

    src = cwd / OUTDIR / ("gen-%d" % gen) / "dataset_summary.json"
    if not src.is_file():
        sys.exit("[ERROR] dataset_build 没产出 dataset_summary.json")
    shutil.copyfile(str(src), str(cwd / OUTDIR / "dataset_summary.json"))
    summ = json.loads(src.read_text())
    print("[DONE] 第 %d 代数据集：%d 帧（train %d / test %d / filtered %d）"
          % (gen, summ["n_frames_total"], summ["n_train"], summ["n_test"],
             summ["n_filtered"]))


if __name__ == "__main__":
    main()
