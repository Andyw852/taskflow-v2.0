#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step1_summary.py —— 新技能模板里的示例 gen 脚本（run: gen 步）。

跑在哪：超算登录节点，**cwd = 本步骤目录**（材料/<技能>/step1_summary/），
        由 tf 的 remote_gen 推送并执行；不排 SLURM 队。
干什么：从上游产物里读数 → 写 template_summary.json（= skill.yaml 的 done_marker）。
判完成：tf 看到 done_marker 存在就把该步标 done，并把产物拉回本地 result/。

本模板故意只带一个"后处理步"，让它**开箱就能跑通/看懂**（不依赖超算排队）。
要加真正提交 VASP 的计算步时，照这两份抄：
  · skill/opt-dft-cpu/gen_step2_static.py  —— 写 INCAR/KPOINTS/submit.sh 的完整套路
  · skill/opt-dft-cpu/templates/           —— incar_*.tpl 模板（@占位符@ 替换）
  提交脚本模板在 setting/<集群>/templates/submit_*.tpl（站点相关，不随技能走）。

命令行自测（本地随便找个目录放个 POSCAR/OUTCAR）：
    python3 gen_step1_summary.py            # 写 ./template_summary.json
"""
import json
import os
import re

OUT_FILE = "template_summary.json"
# 上游步骤目录候选（cwd 找不到 OUTCAR 时往上一步的目录里找）
UPSTREAM_DIRS = ("step1_static", "step1_opt", ".")


def find_outcar():
    for d in UPSTREAM_DIRS:
        p = os.path.join(d, "OUTCAR")
        if os.path.isfile(p):
            return p
    return None


def parse_outcar(path):
    """从 OUTCAR 里抠两个最常要的数：总能、是否收敛（按需换成你自己的解析）。"""
    energy, converged, n_ions = None, False, None
    with open(path, errors="ignore") as f:
        for line in f:
            m = re.search(r"free\s+energy\s+TOTEN\s*=\s*(-?\d+\.\d+)", line)
            if m:
                energy = float(m.group(1))          # 取最后一个 = 最终值
            if "reached required accuracy" in line:
                converged = True
            m = re.search(r"ions per type\s*=\s*(.*)", line)
            if m:
                n_ions = sum(int(x) for x in m.group(1).split())
    return {"energy_eV": energy, "converged": converged, "n_atoms": n_ions}


def main():
    src = find_outcar()
    out = {"step": "step1_summary", "source": src}
    if src:
        out.update(parse_outcar(src))
    else:
        # 没有上游产物：照样产出文件（done_marker 必须存在），但把状态写清楚，
        # 免得 tf 显示 done 而人以为真算完了。
        out.update({"energy_eV": None, "converged": False, "n_atoms": None,
                    "warning": "没找到 OUTCAR（%s）—— 这是模板骨架的正常表现"
                               % "/".join(UPSTREAM_DIRS)})
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("已写 %s：%s" % (OUT_FILE, json.dumps(out, ensure_ascii=False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
