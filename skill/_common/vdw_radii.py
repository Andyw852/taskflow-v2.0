#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill/_common/vdw_radii.py —— 范德华半径单一真源。

历史：ke-dft-cpu（step8_amset）和 kl-dft-cpu（step6_kappa）各自内置过一份表，两份有 15 个元素
取值不同（Zn 1.39 vs 2.01、Ag 1.72 vs 2.11、Hg 1.55 vs 2.23 A 等，多为过渡
金属；S/Se/Te 一致，所以 TMD 没受影响）。两边算 2D 层厚用的是同一个公式
d = zspan + vdW(top) + vdW(bot)，表不同则 d 不同，kappa_L 与 sigma 的厚度口径
就会错开。收到这里之后两边读同一个对象，不可能再漂。

取值：Bondi (1964) + Alvarez (2013)，单位 A。故意用纯字典而不依赖 pymatgen——
gen 脚本在登录节点用系统 python 跑，公共池的设计就是不引入重依赖。
"""

VDW_RADII = {
    "H": 1.20, "He": 1.40, "Li": 1.82, "Be": 1.53, "B": 1.92, "C": 1.70,
    "N": 1.55, "O": 1.52, "F": 1.47, "Ne": 1.54, "Na": 2.27, "Mg": 1.73,
    "Al": 1.84, "Si": 2.10, "P": 1.80, "S": 1.80, "Cl": 1.75, "Ar": 1.88,
    "K": 2.75, "Ca": 2.31, "Sc": 2.15, "Ti": 2.11, "V": 2.07, "Cr": 2.06,
    "Mn": 2.05, "Fe": 2.04, "Co": 2.00, "Ni": 1.97, "Cu": 1.96, "Zn": 2.01,
    "Ga": 1.87, "Ge": 2.11, "As": 1.85, "Se": 1.90, "Br": 1.85, "Kr": 2.02,
    "Rb": 3.03, "Sr": 2.49, "Y": 2.32, "Zr": 2.23, "Nb": 2.18, "Mo": 2.17,
    "Tc": 2.16, "Ru": 2.13, "Rh": 2.10, "Pd": 2.10, "Ag": 2.11, "Cd": 2.18,
    "In": 1.93, "Sn": 2.17, "Sb": 2.06, "Te": 2.06, "I": 1.98, "Xe": 2.16,
    "Cs": 3.43, "Ba": 2.68, "La": 2.43, "Ce": 2.42, "Pr": 2.40, "Nd": 2.39,
    "Sm": 2.36, "Eu": 2.35, "Gd": 2.34, "Tb": 2.33, "Dy": 2.31, "Ho": 2.30,
    "Er": 2.29, "Tm": 2.27, "Yb": 2.26, "Lu": 2.24, "Hf": 2.23, "Ta": 2.22,
    "W": 2.18, "Re": 2.16, "Os": 2.16, "Ir": 2.13, "Pt": 2.13, "Au": 2.14,
    "Hg": 2.23, "Tl": 1.96, "Pb": 2.02, "Bi": 2.07, "Po": 1.97, "At": 2.02,
    "Rn": 2.20, "Th": 2.43, "U": 2.41,
}

VDW_FALLBACK = 2.00      # 表里没有的元素用这个


def get(symbol, default=None):
    """按元素符号取半径；缺失返回 VDW_FALLBACK（或调用方给的 default）。"""
    return VDW_RADII.get(symbol, VDW_FALLBACK if default is None else default)
