#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""write_step1_summary.py —— step1 完成标记（在计算节点、作业内由 run_graph_data.sh 调用）。

检查 graph_data_non_soc/graph_data.npz（与 SOC 时的 graph_data_soc/graph_data.npz）
是否就绪，写 graph_data_summary.json。判据看其中的 "GRAPH_DATA_DONE": true。
"""
import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--soc", action="store_true",
                    help="SOC 通用模型：额外要求 graph_data_soc/graph_data.npz")
    args = ap.parse_args()

    ok = os.path.isfile("graph_data_non_soc/graph_data.npz")
    soc_ok = os.path.isfile("graph_data_soc/graph_data.npz") if args.soc else True
    done = bool(ok and soc_ok)
    json.dump({"GRAPH_DATA_DONE": done, "soc": args.soc,
               "non_soc_npz": "graph_data_non_soc/graph_data.npz",
               "soc_npz": ("graph_data_soc/graph_data.npz" if args.soc else None)},
              open("graph_data_summary.json", "w"), indent=2)
    if not done:
        raise SystemExit("[ERROR] graph_data.npz 缺失：non_soc=%s soc=%s"
                         % (ok, soc_ok))


if __name__ == "__main__":
    main()
