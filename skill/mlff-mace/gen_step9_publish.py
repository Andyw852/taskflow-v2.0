#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step9_publish.py —— mlff-mace step9：发布（run: gen，仅 status==pass 才拷模型）。

运行位置：超算登录节点，cwd = 材料目录。

    status == pass  → 把 seed-1 模型拷进 MACE_MODEL_DIR/<材料>_ft.model + model_card.json
    status == halt* → 写 model_card.json（converged: false + 未通过的闸 + 已知局限），
                      绝不把没收敛的模型当通过发布（§7.4）
    其它（expand 等）→ 写 publish_skip.json 说明还没到发布阶段

写：step9_publish/{publish_status.json, model_card.json?, <材料>_ft.model?}
退出码 0 成功；非 0 = [ERROR]。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc
import stepconf

OUTDIR = "step9_publish"
STEP = "step9_publish"


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)
    cv = dict(conf.params)

    val = mc.read_json(cwd / "step8_benchmark" / "validation_summary.json")
    if not val:
        sys.exit("[ERROR] step8 的 validation_summary.json 不存在 —— 先让 S8 跑完。")
    status = val.get("status", "expand")
    gen = int(val.get("generation", 0))
    mat = sys.argv[1] if len(sys.argv) > 1 else cwd.parent.name
    hist = mc.conv_history()
    model_src = cwd / "step7_finetune" / "seed-1" / \
        ("%s_gen%d_seed1.model" % (mat, gen))

    card = {
        "material": mat,
        "dim": val.get("dim"),
        "generation": gen,
        "converged": status == "pass",
        "status": status,
        "model": ("%s_ft.model" % mat) if status == "pass" else None,
        "rmse_THz": val.get("phonon_rmse_THz"),
        "gates": val.get("gates"),
        "fingerprint": mc.read_json(cwd / "step6_dataset" / "dataset_summary.json", {}).get("fingerprint"),
        "n_frames": val.get("n_frames_total"),
        "history": hist,
        "limitations": [
            "本技能验收的是势在谐波与准谐层面可信；fc3 与 κ 的最终可信度要靠下游"
            "kl-mace-* 跑出与已知参考对照，本技能不替它背书",
            "磁性体系：MACE 不含自旋自由度，模型只对训练时的磁序有效",
        ],
        "downstream_MACE_MODEL": ("/public/home/wangchao/software/mace/mace_models/%s_ft.model" % mat)
        if status == "pass" else None,
    }
    if val.get("dim") == "2d":
        card["limitations"].insert(0, "2D：未考虑 LO-TO 劈裂（未加 NAC）；面外应力未参与训练")

    status_json = {"status": status, "generation": gen, "converged": status == "pass"}
    if status == "pass":
        if not model_src.is_file():
            sys.exit("[ERROR] status=pass 但 seed-1 模型不存在：%s" % model_src)
        dst_dir = Path(cv["MACE_MODEL_DIR"] or
                       "/public/home/wangchao/software/mace/mace_models")
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / ("%s_ft.model" % mat)
        shutil.copyfile(str(model_src), str(dst))
        shutil.copyfile(str(model_src), str(out / ("%s_ft.model" % mat)))
        card["published_path"] = str(dst)
        status_json["published_path"] = str(dst)
        print("[OK] 已发布 %s（status=pass）" % dst)
    elif str(status).startswith("halt_"):
        card["note"] = ("停机（%s）：模型未发布。保留全部数据与最后一代模型在 "
                        "step7_finetune/seed-*。排查清单见 step8 validation_summary / "
                        "convergence_history.json。" % status)
        status_json["note"] = card["note"]
    else:
        note = ("status=%s：还没到发布阶段。按 README 手动推进下一代："
                "tf conf --set params.GENERATION=%d → tf -j 4 rerun → tf start。"
                % (status, gen + 1))
        (out / "publish_skip.json").write_text(
            json.dumps({"status": status, "note": note}, ensure_ascii=False, indent=2)
            + "\n", encoding="utf-8", newline="\n")
        status_json["note"] = note

    if status == "pass" or str(status).startswith("halt_"):
        (out / "model_card.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
    (out / "publish_status.json").write_text(
        json.dumps(status_json, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("[DONE] %s：status=%s" % (OUTDIR, status))


if __name__ == "__main__":
    main()
