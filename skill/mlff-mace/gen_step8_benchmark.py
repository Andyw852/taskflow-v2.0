#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step8_benchmark.py —— mlff-mace step8：全部验收闸 + 学习曲线 + 决策表（sbatch）。

运行位置：超算登录节点，cwd = 材料目录；只写 submit.sh，重活由 benchmark.py 在
计算节点作业里干（作业里再调 learning_curve.py / converge_ctrl.py）。

读：step7 seed-* 模型 + step6 数据集 + step4 清单 + step5 displ/static 帧力 + step1
写：step8_benchmark/gen-<K>/validation_summary.json + 五张图 + results_<材料>.txt +
    learning_curve.json + plan.json；convergence_history.json 顶层逐代追加
    （顶层 validation_summary.json 副本 = 判据/done_marker）
退出码 0 成功；非 0 = [ERROR]。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc
import stepconf

OUTDIR = "step8_benchmark"
STEP = "step8_benchmark"


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)
    cv = dict(conf.params)

    ds_sum = mc.read_json(cwd / "step6_dataset" / "dataset_summary.json")
    if not ds_sum:
        sys.exit("[ERROR] step6 数据集不存在 —— 先让 S6 跑完。")
    gen = int(ds_sum["generation"])
    man = mc.read_json(cwd / "step4_genstruct" / "struct_manifest.json")
    if not man or int(man["generation"]) != gen:
        sys.exit("[ERROR] step4 清单代数(%s)与数据集代数(%d)不一致。"
                 % (man and man.get("generation"), gen))

    method = mc.read_kv(cwd / "step1_relax" / mc.METHOD_FILE)
    dim = (method.get("DIM") or "3d").lower()
    if dim == "2d":
        e_w, s_w = 10.0, 0.0
    else:
        # 与 step7 一致：3D 应力权重用 STRESS_WEIGHT_3D（autoplex 默认 1.0），
        # 不能硬编码 10.0 —— 否则学习曲线的子集模型和「all 点」（复用 S7 模型）超参不一致
        e_w, s_w = 1.0, cv["STRESS_WEIGHT_3D"] or 1.0
    if str(cv["ENERGY_WEIGHT"]).strip().lower() != "auto":
        e_w = cv["ENERGY_WEIGHT"]
    if str(cv["STRESS_WEIGHT"]).strip().lower() != "auto":
        s_w = cv["STRESS_WEIGHT"]

    mat = sys.argv[1] if len(sys.argv) > 1 else cwd.parent.name
    seed_dirs = ",".join(str(cwd / "step7_finetune" / ("seed-%d" % s))
                         for s in range(1, int(cv["N_COMMITTEE"]) + 1))
    model = cwd / "step7_finetune" / "seed-1" / ("%s_gen%d_seed1.model" % (mat, gen))
    if not model.is_file():
        sys.exit("[ERROR] seed-1 模型不存在：%s —— 先让 S7 跑完。" % model)

    # 基座/REPLAY 解析成绝对路径（作业 cwd=step8_benchmark/）
    from mace_model import resolve_model as _rm
    _kind, _val = _rm(cv["MACE_MODEL"] or "mace-mp:medium",
                      [str(cwd), cv["MACE_MODEL_DIR"] or ""])
    if _kind != "file":
        sys.exit("[ERROR] MACE_MODEL=%r 解析成了基座名（%s）。学习曲线/微调必须指向"
                 " .model 文件。" % (cv["MACE_MODEL"], _val))
    foundation = _val
    replay = cv["REPLAY_XYZ"] or ""
    if replay:
        rp = Path(replay)
        if not rp.is_absolute():
            rp = Path.cwd() / rp
        if not rp.is_file():
            sys.exit("[ERROR] REPLAY_XYZ=%s 不存在。" % rp)
        replay = str(rp)

    cmd = ("python ../benchmark.py "
           "--gen %d --mat %s --matdir %s --out gen-%d "
           "--model '%s' --seed-dirs '%s' "
           "--train-xyz '%s' --test-xyz '%s' "
           "--manifest '%s' --step5-dir step5_label --step1-dir step1_relax "
           "--sc-summary step2_supercell/supercell_summary.json "
           "--dim %s --device %s --rmse-max %g --ref-disp %g --grun-strain %g "
           "--curve-points '%s' --curve-tol %g "
           "--foundation '%s' --replay '%s' --e0s '%s' "
           "--epochs %d --batch-size %d --lr %g --patience %d --start-swa %d --loss %s "
           "--energy-weight %g --forces-weight %g --stress-weight %g "
           "--ref-fc2-path '%s'"
           % (gen, mat, cwd, gen, model, seed_dirs,
              cwd / "step6_dataset" / ("gen-%d" % gen) / "train.xyz",
              cwd / "step6_dataset" / ("gen-%d" % gen) / "test.xyz",
              cwd / "step4_genstruct" / "struct_manifest.json",
              dim, cv["DEVICE"] or "auto", cv["RMS_MAX"], cv["REF_DISP"],
              cv["GRUNEISEN_STRAIN"], cv["CURVE_POINTS"], cv["CURVE_TOL"],
              foundation, replay,
              cwd / "step6_dataset" / ("gen-%d" % gen) / "e0s.json",
              cv["EPOCHS"], cv["BATCH_SIZE"], cv["LR"],
              cv["PATIENCE"], cv["START_SWA"], cv["LOSS"] or "huber",
              e_w, cv["FORCES_WEIGHT"], s_w, cv["REF_FC2_PATH"] or ""))

    here = Path(__file__).resolve().parent
    tpl = mc.resolve_submit(here, "submit_mace")
    mc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": mc.new_jobname(cwd, "bench", tag="mlff")[:80],
                     "CONDA_SH": cv["CONDA_SH"],
                     "CONDA_ENV": cv["CONDA_ENV"],
                     "MACE_CMD": cmd})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪（gen %d；作业跑完写 validation_summary.json）"
          % (OUTDIR, gen))


if __name__ == "__main__":
    main()
