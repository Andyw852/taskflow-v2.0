#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S1_features: 读材料目录 POSCAR -> 计算 14 维易算特征 -> step1_features/te_features.json

纯标准库 + numpy，登录节点秒级，无 DFT。
"""
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cheap_features as CF

OUTDIR = "step1_features"
poscar = os.path.join(os.getcwd(), "POSCAR")
if not os.path.isfile(poscar):
    sys.exit("[ERROR] POSCAR not found; te-screen needs an input structure at <material>/POSCAR")

with open(poscar) as f:
    txt = f.read()
props = CF.load_element_properties("element_properties.json")
feat, meta = CF.compute_features(txt, props)

os.makedirs(OUTDIR, exist_ok=True)
out = {"n_sites": feat["nsites"], "features": feat, "species": meta["species"]}
with open(os.path.join(OUTDIR, "te_features.json"), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("[OK] features -> %s  n_sites=%s" % (OUTDIR, feat["nsites"]))

