#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""decide_discriminant.py —— 分析步：写出 step2_bandgap/step2.15_discriminant/discriminant.json

由 skill.yaml 的 seq 2.155（run: gen, done_marker: discriminant.json）自动调用，
与能带 plot 步同一套模式：纯分析，不提交 HPC。

产出被三处消费：
  1) 下游阻断：step5_dielect / step8_amset / step8.1_boltztrap 的 gen 读 block.blocked
     （可被 FORCE_TRANSPORT = True 覆盖，不是硬停）；
  2) step7b_read 给 band_edges.json 标 validity（带重叠时带边是形式上的）；
  3) efermi_eV —— step2.1/2.2 的 E_F 是粗网格偏值，用它对齐能带。

注意杂化步的 ALGO **不读本文件**（循环依赖），它直接读 OUTCAR 现算。

用法：
    python decide_discriminant.py
    python decide_discriminant.py --hybrid-outcar ../step2.3_hse_pbe0/OUTCAR   # 跑完杂化后覆盖
    python decide_discriminant.py --functional HSE06 --hybrid-outcar <OUTCAR>
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discriminant_common as dc  # noqa: E402

DISC_DIR = "step2_bandgap/step2.15_discriminant"
HYBRID_CANDS = [
    "step2_bandgap/step2.3_hse_pbe0/OUTCAR",
    "step2_bandgap/step2.3_hse/OUTCAR",
]


def main():
    ap = argparse.ArgumentParser(description="写体系判定的 discriminant.json")
    ap.add_argument("--job-dir", default=None, help="工程根目录，默认当前目录")
    ap.add_argument("--functional", default=None,
                    help="强制 label 里用的泛函名（用于判别静态本身就是杂化时）")
    ap.add_argument("--hybrid-outcar", default=None,
                    help="杂化步 OUTCAR；给了就额外并入一条 @杂化 判定")
    ap.add_argument("--no-auto-hybrid", action="store_true",
                    help="不去自动找杂化 OUTCAR")
    args = ap.parse_args()

    cwd = Path(args.job_dir).expanduser().resolve() if args.job_dir else Path.cwd()
    d = cwd / DISC_DIR
    oc = d / "OUTCAR"
    if not oc.is_file():
        sys.exit("[ERROR] 缺 %s/OUTCAR —— step2.15_discriminant 没跑完？" % DISC_DIR)

    # 解析失败必须硬失败（不许静默返回 None 让判定继续）——见 DiscriminantParseError
    try:
        cur = dc.decide_from_outcar(oc, dc.read_mesh(d), args.functional)
    except dc.DiscriminantParseError as e:
        sys.exit("[ERROR] %s" % e)
    verdicts = [cur]

    if args.hybrid_outcar or not args.no_auto_hybrid:
        cands = ([args.hybrid_outcar] if args.hybrid_outcar else HYBRID_CANDS)
        for cand in cands:
            p = Path(cand).expanduser()
            p = p if p.is_absolute() else (cwd / p)
            if not p.is_file():
                continue
            h = dc.decide_from_outcar(p, None, None)
            if h:
                h["role"] = "hybrid_override"
                verdicts.append(h)
                print("[OK] 并入杂化判定：%s@%s gap=%+.4f eV（%s）"
                      % (h["label"], h["functional"], h["gap_eV"], p))
                break
        else:
            if args.hybrid_outcar:
                print("[WARN] 指定的 --hybrid-outcar 不存在或解析失败：%s"
                      % args.hybrid_outcar)

    payload = dc.build_payload(verdicts)
    out = d / dc.OUT_JSON
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8", newline="\n")
    print("[DONE] %s" % out)
    print("       label=%s  gap=%+.4f eV  ALGO 建议=%s  blocked=%s (decisive=%s)"
          % (payload["label"], payload["effective"]["gap_eV"],
             payload["algo_recommendation"], payload["block"]["blocked"],
             payload["block"]["decisive"]))
    if payload["block"]["hint"]:
        print(payload["block"]["hint"])


if __name__ == "__main__":
    main()
