#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""defect-dft-cpu step0（run:gen）：凸包参考相（元素相 + 竞争二元相）生成/提交/收集能量。

幂等 + 4 材料共享：参考相只在 REFERENCES_DIR（step.conf）算一份。首次生成+提交；
之后每轮 watch 检查收敛，全收敛则收集能量写 references_energy.json（done_marker）。

输出 references_energy.json：
  { "elements": {"Pb": 每原子eV, ...}, "binaries": {"PbTe": {"formula": {...}, "E_per_fu": eV}, ...} }
"""
import sys, os, json, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import defects_common as D
from gen_references import PHASES, INCAR

STEP = "step0_references"

# 元素相 -> 元素符号（能量归一化到每原子）
ELEMENT_EL = {"Pb_fcc": "Pb", "Sn_beta": "Sn", "Sb_rhombo": "Sb",
              "Bi_rhombo": "Bi", "Te_trig": "Te"}
# 二元相 -> 式量信息（能量归一化到每式量）
BINARY = {
    "PbTe_rs": {"name": "PbTe",   "formula": {"Pb": 1, "Te": 1}, "fu": 1},
    "SnTe_rs": {"name": "SnTe",   "formula": {"Sn": 1, "Te": 1}, "fu": 1},
    "Sb2Te3":  {"name": "Sb2Te3", "formula": {"Sb": 2, "Te": 3}, "fu": 3},
    "Bi2Te3":  {"name": "Bi2Te3", "formula": {"Bi": 2, "Te": 3}, "fu": 3},
}

def ref_energy(outcar):
    """取 OUTCAR 最后 without entropy（E0）。"""
    if not os.path.exists(outcar):
        return None
    e = None
    for line in open(outcar, errors="ignore"):
        if "without entropy" in line:
            try:
                e = float(line.split("=")[1].split()[0])
            except (ValueError, IndexError):
                pass
    return e

def main():
    conf = D.load_stepconf()
    refdir = Path(conf.get("REFERENCES_DIR", "/public/home/wangchao/convex_hull_refs"))
    potdir = conf.get("POTCAR_DIR", "/public/home/wangchao/software/vasp_pseudopotentials")
    user = conf.get("USER", os.environ.get("USER", "wangchao"))
    posdir = refdir / "convex_hull_references"
    os.makedirs(STEP, exist_ok=True)

    # 1) 生成 + 提交（幂等：已有 INCAR 不重生成；已在队列/跑过不重提交）
    for name, spec in PHASES.items():
        d = posdir / name
        if not (d / "POSCAR").exists():
            print("[跳过] %s 缺 POSCAR（请放入 %s）" % (name, d))
            continue
        d.mkdir(parents=True, exist_ok=True)
        if not (d / "INCAR").exists():
            st = D.parse_poscar(str(d / "POSCAR"))
            order = []
            for a in st["atoms"]:
                if a not in order:
                    order.append(a)
            natoms = len(st["atoms"])
            incar = (INCAR.replace("{{name}}", name)
                         .replace("{{ismear}}", str(spec["ismear"]))
                         .replace("{{sigma}}", str(spec["sigma"]))
                         .replace("{{magmom}}", "%d*0" % (3 * natoms)))
            (d / "INCAR").write_text(incar, encoding="utf-8")
            D.write_kpoints(str(d / "KPOINTS"), spec["kmesh"])
            D.assemble_potcar(order, potdir, out_path=str(d / "POTCAR"))
            D.render_submit(D.find_submit_tpl(True), str(d / "submit.sh"), "ref_" + name)
        if not (d / "OUTCAR").exists():
            q = subprocess.run(["squeue", "-u", user, "-h", "-o", "%.12j"],
                               capture_output=True, text=True).stdout
            if ("ref_" + name) not in q:
                subprocess.run(["sbatch", "submit.sh"], cwd=str(d))

    # 2) 检查收敛 + 收集能量
    energies = {}
    all_done = True
    for name in PHASES:
        d = posdir / name
        e = ref_energy(str(d / "OUTCAR"))
        if e is None:
            print("  %-11s 尚未出结果" % name)
            all_done = False
            continue
        conv = "reached required accuracy" in open(str(d / "OUTCAR"), errors="ignore").read()
        if not conv:
            all_done = False
        energies[name] = e
        print("  %-11s E0=%.6f eV  收敛=%s" % (name, e, conv))

    if all_done:
        out = {"elements": {}, "binaries": {}}
        for name, e in energies.items():
            if name in ELEMENT_EL:
                st = D.parse_poscar(str(posdir / name / "POSCAR"))
                out["elements"][ELEMENT_EL[name]] = e / len(st["atoms"])
            elif name in BINARY:
                out["binaries"][BINARY[name]["name"]] = {
                    "formula": BINARY[name]["formula"],
                    "E_per_fu": e / BINARY[name]["fu"],
                }
        json.dump(out, open(refdir / "references_energy.json", "w"), indent=2, ensure_ascii=False)
        json.dump(out, open(os.path.join(STEP, "references_energy.json"), "w"),
                  indent=2, ensure_ascii=False)
        print("[OK] 参考相全部收敛，能量已写 %s" % (refdir / "references_energy.json"))
    else:
        print("[进行中] 参考相未全收敛，下次 watch 自动再查")

if __name__ == "__main__":
    main()
