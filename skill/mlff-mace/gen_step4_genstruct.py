#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step4_genstruct.py —— mlff-mace step4：生成本代全部待标注构型（run: gen）。

运行位置：超算登录节点，cwd = 材料目录。重活由 rattle_gen.py 在 venv 里干。

干的事（按顺序）：
    1. 停机守卫：GENERATION > MAX_GENERATION 硬停；convergence_history 的 halt_*
       且未设 FORCE_CONTINUE → 拒绝推进（§7.4）
    2. RATTLE_STD=auto → 读 step3 自校准三档；显式值 → 原样用
    3. extend 模式（gen0）：对 PRE_XYZ_FILES 做覆盖分析 → coverage_plan.json
       （把新采样压到应变/幅度盲区，见 dataset_build --coverage-only）
    4. rattle_gen.py 生成 static/rattle/displ/iso 帧 + struct_manifest.json
       （displ 与 iso 只在第 0 代生成；给了 REF_FC2_PATH 跳过 displ）
写：step4_genstruct/gen-<K>/struct_manifest.json + 顶层同名副本（done_marker）
退出码 0 成功；非 0 = [ERROR]。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc
import stepconf

OUTDIR = "step4_genstruct"
STEP = "step4_genstruct"


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)
    cv = dict(conf.params)

    # ---- 停机守卫（§7.4）----
    gen, gmax = mc.halt_guard(conf, cwd)
    print("[..] 第 %d 代（MAX_GENERATION=%d）" % (gen, gmax))

    # ---- 上游接力检查 ----
    for need in ("step1_relax/CONTCAR", "step1_relax/OUTCAR",
                 "step2_supercell/supercell_summary.json",
                 "step3_calib/calib_summary.json"):
        if not (cwd / need).is_file():
            sys.exit("[ERROR] 缺 %s —— 前序步骤没跑完。" % need)

    # ---- RATTLE_STD 三档（auto = step3 自校准）----
    calib = json.loads((cwd / "step3_calib/calib_summary.json").read_text())
    rattle_std = cv["RATTLE_STD"]
    if str(rattle_std).strip().lower() == "auto":
        rattle_std = calib.get("rattle_std_A")
        if not rattle_std:
            sys.exit("[ERROR] step3 的 calib_summary.json 里没有 rattle_std_A。")
        rattle_std = ",".join(str(x) for x in rattle_std)
        if calib.get("fallback_used"):
            print("[WARN] 用 fallback 幅度（基座模型有虚频）：%s" % calib.get("warning"))
    print("[..] RATTLE_STD = %s" % rattle_std)

    # ---- 维度（继承 workflow_method.txt）----
    method = mc.read_kv(cwd / "step1_relax" / mc.METHOD_FILE)
    dim = (method.get("DIM") or "3d").lower()
    if dim == "0d":
        sys.exit("[ERROR] 0D 体系不支持。")
    sc_sum = json.loads((cwd / "step2_supercell/supercell_summary.json").read_text())

    # ---- extend（gen0）：覆盖分析，新采样压到盲区 ----
    plan_arg = ""
    if gen == 0 and cv["DATA_MODE"] == "extend" and cv["PRE_XYZ_FILES"]:
        rc = mc.run_py("dataset_build.py",
                       "--gen %d --coverage-only --dim %s --pre-xyz '%s' "
                       "--vol-factors '%s' --n-per-cell %d --outdir %s/gen-%d"
                       % (gen, dim, cv["PRE_XYZ_FILES"], cv["VOL_FACTORS"],
                          cv["N_PER_CELL"], OUTDIR, gen),
                       cwd, conf=cv, logname="coverage.log")
        if rc != 0:
            sys.exit("[ERROR] 覆盖分析失败（rc=%d），看 coverage.log。" % rc)
        plan_path = cwd / OUTDIR / ("gen-%d" % gen) / "coverage_report.json"
        if plan_path.is_file():
            rep = json.loads(plan_path.read_text())
            plan = rep.get("sampling_plan")
            if plan and plan.get("targets"):
                print("[..] 覆盖盲区加采计划：%s" % plan["reason"])
                plan_arg = "--plan '%s'" % json.dumps(plan, ensure_ascii=False)

    # ---- 生成结构 ----
    args = ("--gen %d --outdir %s/gen-%d --prim step1_relax/CONTCAR "
            "--dim %s --vol-factors '%s' --rattle-std '%s' --n-per-cell %d "
            "--min-dist-ratio %g --ref-disp %g --iso-box %g --grun-strain %g "
            "--seed-base %d --gen-increment %d --ref-fc2-path '%s' %s"
            % (gen, OUTDIR, gen, dim, cv["VOL_FACTORS"], rattle_std,
               cv["N_PER_CELL"], cv["MIN_DIST_RATIO"], cv["REF_DISP"],
               cv["ISO_BOX"], cv["GRUNEISEN_STRAIN"], cv["SEED_BASE"],
               cv["GEN_INCREMENT"], cv["REF_FC2_PATH"] or "", plan_arg))
    rc = mc.run_py("rattle_gen.py", args, cwd, conf=cv, logname="rattle_gen.log")
    if rc != 0:
        sys.exit("[ERROR] rattle_gen.py 失败（rc=%d），看 rattle_gen.log 尾部。" % rc)

    # ---- 顶层副本（done_marker / 判据 / 下游 gen 读它）----
    src = cwd / OUTDIR / ("gen-%d" % gen) / "struct_manifest.json"
    if not src.is_file():
        sys.exit("[ERROR] rattle_gen 没产出 struct_manifest.json")
    shutil.copyfile(str(src), str(cwd / OUTDIR / "struct_manifest.json"))
    man = json.loads(src.read_text())
    print("[DONE] 第 %d 代共 %d 帧（rattle %d + displ %d + static %d + iso %d）"
          % (gen, man["n_frames"], man["n_rattle"], man["n_displ"],
             man["n_static"], man["n_iso"]))


if __name__ == "__main__":
    main()
