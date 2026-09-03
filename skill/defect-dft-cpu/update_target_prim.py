#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update_target_prim.py —— 把独立 ISIF=3 密网格原胞总能写入 references_energy.json 的 target_prim。

凸包的目标相 ΔH_f 必须用与参考相同口径（ISIF=3 + 密 k 网格）的原胞能量，
不能用 step1_bulk 超胞/9（ISIF=2 + 2x2x1 粗网格）。本脚本负责这个口径修复。

用法：python3 update_target_prim.py [原胞目录] [formula_json]
  原胞目录默认 convex_hull_references/Sn2Sb2Te5_p8
"""
import sys, os, json

def read_energy(outcar):
    """取 OUTCAR 最后 without entropy（E0）。"""
    if not os.path.exists(outcar):
        return None
    e = None
    for line in open(outcar, errors='ignore'):
        if 'without entropy' in line:
            try:
                e = float(line.split('=')[1].split()[0])
            except (ValueError, IndexError):
                pass
    return e

def main():
    prim_dir = sys.argv[1] if len(sys.argv) > 1 else 'convex_hull_references/Sn2Sb2Te5_p8'
    formula = {'Sn': 2, 'Sb': 2, 'Te': 5}   # 225 原胞 = 1 式量
    outcar = os.path.join(prim_dir, 'OUTCAR')
    if not os.path.exists(outcar):
        raise SystemExit('[错误] 找不到 %s（先跑完原胞 ISIF=3 计算）' % outcar)
    if 'reached required accuracy' not in open(outcar, errors='ignore').read():
        print('[警告] %s 尚未显示收敛标记，仍取最后能量（请确认收敛）' % outcar)
    E0 = read_energy(outcar)
    if E0 is None:
        raise SystemExit('[错误] %s 无能量' % outcar)
    ref_path = 'references_energy.json'
    for cand in ('step0_references/references_energy.json', 'references_energy.json'):
        if os.path.exists(cand):
            ref_path = cand
            break
    ref = json.load(open(ref_path, encoding='utf-8'))
    ref['target_prim'] = {'formula': formula, 'E_per_fu': round(E0, 8), 'dir': prim_dir}
    json.dump(ref, open(ref_path, 'w'), indent=2, ensure_ascii=False)
    print('[OK] target_prim = %.8f eV/式量 (%s) -> %s' % (E0, prim_dir, ref_path))
    print('     原胞(ISIF=3,密网格) E_per_fu = %.8f' % E0)

if __name__ == '__main__':
    main()