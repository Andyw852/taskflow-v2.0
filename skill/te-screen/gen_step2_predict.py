#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S2_predict: 读 S1 特征 -> Ridge 替代模型预测 -> step2_predict/te_screen_summary.json"""
import os, sys, json
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cheap_features as CF

MODELS = ["n_ZT_e", "p_ZT_e", "n_log10_PF", "p_log10_PF"]
feat_file = os.path.join(os.getcwd(), "step1_features", "te_features.json")
if not os.path.isfile(feat_file):
    sys.exit("[ERROR] step1_features/te_features.json missing; run S1 first")
with open(feat_file) as f:
    feat = json.load(f)["features"]

pred = {}
for name in MODELS:
    with open(name + ".json") as f:
        blob = json.load(f)
    x = np.array([feat.get(k, np.nan) for k in blob["features"]], dtype=float)
    x = (x - np.array(blob["feature_mean"])) / np.array(blob["feature_scale"])
    x = np.nan_to_num(x, nan=0.0)
    pred[name] = float(np.dot(x, np.array(blob["coef"])) + blob["intercept"])

merit_p = 0.7 * pred["p_ZT_e"] + 0.3 * pred["p_log10_PF"]
merit_n = 0.7 * pred["n_ZT_e"] + 0.3 * pred["n_log10_PF"]
OUTDIR = "step2_predict"
os.makedirs(OUTDIR, exist_ok=True)
out = {"PREDICTED": True, "prediction": pred,
       "merit_p": merit_p, "merit_n": merit_n, "merit": max(merit_p, merit_n)}
with open(os.path.join(OUTDIR, "te_screen_summary.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("[OK] prediction -> %s  %s" % (OUTDIR, json.dumps(pred)))

