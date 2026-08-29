#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""write_step2_summary.py —— step2 完成标记（在计算节点、作业内由 run_predict.sh 调用）。

检查 hamiltonian.npy 是否就绪，写 predict_summary.json。判据看其中的 "PREDICT_DONE": true。
"""
import json
import os


def main():
    ok = os.path.isfile("hamiltonian.npy")
    json.dump({"PREDICT_DONE": ok, "hamiltonian": "hamiltonian.npy"},
              open("predict_summary.json", "w"), indent=2)
    if not ok:
        raise SystemExit("[ERROR] hamiltonian.npy 缺失")


if __name__ == "__main__":
    main()
