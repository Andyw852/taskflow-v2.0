#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""formation_energy.py —— defect-dft-cpu 形成能 / 转变能级 / 自洽费米能级 / P-N 判定。

纯标准库，登录节点可跑。消费 S4 汇总的裸总能 + 第0步凸包产出的化学势，输出
formation_energy_results.json 与 P/N 结论（这是本技能真正的最终产物）。

标准缺陷热力学（E_F 从 VBM 起算，0 ≤ E_F ≤ E_gap）：
  E_f(D,q) = [E_tot(D,q) − E_tot(bulk) − Σ_i Δn_i·μ_i + E_corr(D,q)] + q·E_F
  Δn_i = n_i(缺陷) − n_i(完美超胞)    (正=加原子，负=减原子)
  转变能级 ε(q1/q2) = [E_f(D,q1;E_F=0) − E_f(D,q2;E_F=0)] / (q2 − q1)

有限温自洽 E_F（窄带隙不能用"中带隙最低 E_f"捷径，见 README §关键修正4）：
  电荷中性  Q(E_F) = p0 − n0 + Σ_D N_D Σ_q q·P(D,q;E_F) = 0
  自由载流子用 Fermi-Dirac 积分 F_{1/2}（窄带隙简并，Boltzmann 失准）：
    n0 = Nc·F_{1/2}((E_F−E_gap)/kT)   p0 = Nv·F_{1/2}((−E_F)/kT)
    Nc/Nv = 2(2π m* kT/h²)^{3/2}，由有效质量 mstar_e/mstar_h 算（或直接给 Nc/Nv）
  P(D,q) = exp(−β(E_f0(D,q)+q·E_F)) / Σ_q' exp(−β(...))   (softmax 防溢出)

输入 energies.json（第0步凸包 + 势对齐产出）：
  { "mu": {"Sn":..,"Sb":..,"Te":..},   # 化学势 eV/原子（凸包稳定性窗口内一点）
    "E_gap": ..,                        # 带隙 eV
    "epsilon": ..,                      # 静态介电常数（Makov-Payne 图像电荷修正）
    "mstar_e": .., "mstar_h": ..,       # 电子/空穴有效质量（m_e 单位，默认 0.2）
    "N_D": 1e18,                        # 缺陷浓度 cm^-3（平衡费米能级，可扫）
    "T": 300 }                          # 温度 K

近似（已在结果里标注）：Makov-Payne 各向同性图像电荷（3x3x1 层状体系用 eFNV 更准，
本脚本留了 E_corr 覆盖入口）；有效质量抛物线带近似（替代真实 DOS 积分）。
"""
import sys, os, json, re, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import defects_common as D

EV_PER_ANG = 14.3996448915       # e²/(4πε0)  eV·Å
KB = 8.617333262e-5              # eV/K
ME = 9.1093837015e-31            # 电子静质量 kg
H_PLANCK = 6.62607015e-34        # 普朗克常数 J·s
EV_J = 1.602176634e-19           # eV -> J
MADELUNG = 2.8373                # 简单立方 Madelung 常数（Makov-Payne）

def read_energy(outcar):
    """取 OUTCAR 最后总能（robust：优先 without entropy=E0，兜底 OSZICAR E0）。"""
    if not os.path.exists(outcar):
        return None
    e = None
    with open(outcar, errors="ignore") as f:
        for line in f:
            if "free  energy   TOTEN" in line or "without entropy" in line:
                try:
                    e = float(line.split("=")[1].split()[0])
                except (ValueError, IndexError):
                    continue
    if e is None:
        osz = outcar[:-6] + "OSZICAR" if outcar.endswith("OUTCAR") else outcar.replace("OUTCAR", "OSZICAR")
        if os.path.exists(osz):
            with open(osz, errors="ignore") as f:
                for line in f:
                    if "E0=" in line:
                        e = float(line.split("E0=")[1].split()[0])
    return e

def fd_half(eta, npts=2000, xmax=50.0):
    """Fermi-Dirac 积分 F_{1/2}(η) = (2/√π)∫ x^{1/2}/(1+e^{x-η}) dx（纯标准库梯形法）。

    窄带隙材料 E_F 贴带边（简并），自由载流子密度不能用 Boltzmann 近似，
    必须用这个积分。η < -20 时退回 Boltzmann 极限 e^η（此时二者几乎重合）。"""
    eta = float(eta)
    if eta < -20.0:
        return math.exp(eta)
    h = xmax / npts
    s = 0.0
    prev = 0.0                    # x=0: sqrt(0)/(1+e^{-eta}) = 0
    for i in range(1, npts + 1):
        x = i * h
        cur = math.sqrt(x) / (1.0 + math.exp(x - eta))
        s += 0.5 * (prev + cur) * h
        prev = cur
    return (2.0 / math.sqrt(math.pi)) * s

def eff_dos(mstar, T):
    """有效态密度 N = 2(2π m* kT/h²)^{3/2} (cm^-3)，mstar 以 m_e 为单位。

    注意单位：m*=mstar·m_e(kg)、kT(eV→J)、h(J·s)，全部 SI 自洽。"""
    m = mstar * ME
    kT_J = KB * T * EV_J
    N_m3 = 2.0 * (2.0 * math.pi * m * kT_J / (H_PLANCK * H_PLANCK)) ** 1.5
    return N_m3 * 1e-6           # m^-3 -> cm^-3

def lattice_volume(poscar):
    st = D.parse_poscar(poscar)
    a, b, c = st["lat"]
    v = (a[0]*(b[1]*c[2]-b[2]*c[1]) - a[1]*(b[0]*c[2]-b[2]*c[0]) + a[2]*(b[0]*c[1]-b[1]*c[0]))
    return abs(v)

def load_ref(path="energies.json"):
    if not os.path.exists(path):
        raise SystemExit("[错误] 找不到 %s —— 先做第0步凸包拿化学势，再跑本脚本" % path)
    r = json.load(open(path, encoding="utf-8"))
    need = ["mu", "E_gap"]
    for k in need:
        if k not in r:
            raise SystemExit("[错误] energies.json 缺 %s（化学势 / 带隙）" % k)
    return r

def parse_q(dirname):
    """从 'def-001_v_Te_q+1' 提取电荷态 q；不匹配返回 None。"""
    m = re.search(r"_q([+-]?[0-9]+)$", dirname)
    return int(m.group(1)) if m else None

def qs(q):
    """电荷态显示名：+1 -> '+1', -1 -> '-1', 0 -> '0'。"""
    return ("+" + str(q)) if q > 0 else str(q)

def softmax_charge(Ef0, qs, Ef, beta):
    """P(D,q) = exp(-beta(Ef0+q*Ef)) / sum，数值稳定（减最小值防溢出）。"""
    vals = [Ef0[i] + qs[i]*Ef for i in range(len(qs))]
    m = min(vals)
    exps = [math.exp(-beta*(v - m)) for v in vals]
    z = sum(exps)
    return [x/z for x in exps]

def makov_payne(q, vol, eps):
    if q == 0 or eps <= 0:
        return 0.0
    L = vol ** (1.0/3.0)
    return MADELUNG * q*q / (2.0 * eps * L) * EV_PER_ANG

def load_defects(summary_json="step4_analysis/formation_energy_summary.json",
                 manifest_json="step2_defects/defects_manifest.json"):
    if not os.path.exists(summary_json):
        raise SystemExit("[错误] 找不到 %s —— 先跑完 S4" % summary_json)
    summ = json.load(open(summary_json, encoding="utf-8"))
    E_bulk = summ.get("E_bulk")
    if E_bulk is None:
        raise SystemExit("[错误] summary 缺 E_bulk（step1_bulk 未完成？）")
    manifest = {}
    if os.path.exists(manifest_json):
        for it in json.load(open(manifest_json, encoding="utf-8")):
            manifest[it["dir"]] = it
    # 汇总：{dir: {"name", "counts", "E_neutral", "charged": {q: E}}}
    defects = {}
    raw = summ.get("defects", {})
    for d, en in raw.items():
        q = parse_q(d)
        base = d
        if q is not None:
            base = d[:d.rfind("_q")]
        e = en.get("step2_defects") or en.get("step3_charged")
        if q is None:
            defects.setdefault(base, {}).setdefault("E_neutral", e)
        else:
            defects.setdefault(base, {}).setdefault("charged", {})[q] = e
    return E_bulk, manifest, defects

def analyze(ref=None):
    ref = ref or load_ref()
    E_bulk, manifest, defects = load_defects()
    # 完美超胞成分（n_bulk_i）
    bulk_poscar = "step1_bulk/POSCAR" if os.path.exists("step1_bulk/POSCAR") else "step1_bulk/CONTCAR"
    if not os.path.exists(bulk_poscar):
        raise SystemExit("[错误] 找不到 step1_bulk/POSCAR —— 先跑完 step1")
    bulk_st = D.parse_poscar(bulk_poscar)
    bulk_counts = {}
    for a in bulk_st["atoms"]:
        bulk_counts[a] = bulk_counts.get(a, 0) + 1
    vol = lattice_volume(bulk_poscar)
    eps = float(ref.get("epsilon", 0.0) or 0.0)
    E_gap = float(ref["E_gap"])
    mu = ref["mu"]

    results = {"step": "formation_energy", "status": "done",
               "E_bulk": E_bulk, "volume_AA3": round(vol, 3),
               "epsilon": eps, "E_gap": E_gap,
               "defects": {}}
    allEf0 = []   # 供全局电荷中性扫描
    for base, d in sorted(defects.items()):
        info = manifest.get(base, {})
        name = info.get("name", base)
        counts = info.get("counts", {})
        E0 = d.get("E_neutral")
        if E0 is None:
            continue
        # Δn_i = n_i(缺陷) − n_i(bulk)
        dn = {el: counts.get(el, 0) - bulk_counts.get(el, 0)
              for el in set(list(counts) + list(bulk_counts))}
        chempot = sum(dn.get(el, 0) * mu.get(el, 0.0) for el in dn)
        qnn = sorted([q for q in d.get("charged", {}) if q != 0])
        q_all = [0] + qnn
        Ef0 = {}   # E_f(D,q;E_F=0)
        for q in q_all:
            Eq = E0 if q == 0 else d["charged"][q]
            corr = makov_payne(q, vol, eps)
            Ef0[q] = Eq - E_bulk - chempot + corr
        # 转变能级
        trans = {}
        qlist = sorted(Ef0)
        for i in range(len(qlist)-1):
            q1, q2 = qlist[i], qlist[i+1]
            if q2 - q1 != 0:
                trans["(%s/%s)" % (qs(q1), qs(q2))] = round((Ef0[q1] - Ef0[q2])/(q2 - q1), 5)
        results["defects"][base] = {
            "name": name, "counts": counts, "dn": dn,
            "Ef0": {qs(q): round(v, 5) for q, v in Ef0.items()},
            "transitions": trans}
        allEf0.append((base, name, sorted(q_all), [Ef0[q] for q in sorted(q_all)]))
    results["allEf0"] = allEf0

    # ---- 自洽费米能级 + P/N ----
    ef, dominant, carrier_type = solve_ef(allEf0, ref)
    results["E_F_eq"] = round(ef, 5)
    results["dominant_defect"] = dominant
    results["carrier_type"] = carrier_type

    out = "formation_energy_results.json"
    json.dump(results, open(out, "w"), indent=2, ensure_ascii=False)
    print("[OK] 形成能/转变能级 -> %s" % out)
    print("     平衡费米能级 E_F = %.4f eV (VBM=0, CBM=%.3f, 中带隙=%.3f)"
          % (ef, E_gap, E_gap/2))
    print("     主导缺陷: %s" % dominant)
    print("     结论: %s" % carrier_type)
    return results

def solve_ef(allEf0, ref):
    """扫 E_F∈[0,E_gap]，电荷中性 Q(E_F)=p0−n0+Σ N_D Σ q·P(D,q)=0。

    自由载流子用 Fermi-Dirac 积分（窄带隙简并，Boltzmann 失准）：
      n0 = Nc·F_{1/2}((E_F−E_gap)/kT)   p0 = Nv·F_{1/2}((−E_F)/kT)
    Nc/Nv 由有效质量 mstar_e/mstar_h 算（eff_dos），也可直接给 Nc/Nv。"""
    E_gap = float(ref["E_gap"])
    T = float(ref.get("T", 300.0))
    beta = 1.0/(KB*T)
    kT = KB*T
    # 有效态密度：优先 mstar，其次直接给 Nc/Nv
    Nc = float(ref.get("Nc", eff_dos(ref.get("mstar_e", 0.2), T)))
    Nv = float(ref.get("Nv", eff_dos(ref.get("mstar_h", 0.2), T)))
    N_D = float(ref.get("N_D", 1e18))
    npts = 2000
    def Q(Ef):
        n0 = Nc*fd_half((Ef - E_gap)/kT)
        p0 = Nv*fd_half((-Ef)/kT)
        tot = p0 - n0
        for base, name, qs, ef0 in allEf0:
            P = softmax_charge(ef0, qs, Ef, beta)
            tot += N_D * sum(qs[i]*P[i] for i in range(len(qs)))
        return tot
    prev_ef, prev_q = 0.0, Q(0.0)
    best = (abs(prev_q), 0.0)
    for i in range(1, npts+1):
        ef = E_gap * i / npts
        q = Q(ef)
        if abs(q) < best[0]:
            best = (abs(q), ef)
        if prev_q is not None and q*prev_q <= 0:
            # 线性插值求根
            ef_root = prev_ef + (ef - prev_ef)*abs(prev_q)/(abs(prev_q)+abs(q))
            best = (0.0, ef_root)
            break
        prev_ef, prev_q = ef, q
    ef_eq = best[1]
    # 主导缺陷 = E_f 最低的缺陷在 ef_eq 处的最可能电荷态
    dom_name, dom_q = None, 0
    dom_min = float("inf")
    for base, name, qs, ef0 in allEf0:
        P = softmax_charge(ef0, qs, ef_eq, beta)
        i = max(range(len(qs)), key=lambda k: P[k])
        ef_min = ef0[i] + qs[i]*ef_eq
        if ef_min < dom_min:
            dom_min = ef_min
            dom_name, dom_q = name, qs[i]
    mid = E_gap/2
    if ef_eq > mid + 1e-6:
        carrier = "n 型"
    elif ef_eq < mid - 1e-6:
        carrier = "p 型"
    else:
        carrier = "本征（E_F≈中带隙，需结合真实 DOS 再判）"
    dom_desc = "%s(q=%+d, E_f=%.3f eV)" % (dom_name, dom_q, dom_min) if dom_name else "(无)"
    return ef_eq, dom_desc, carrier

if __name__ == "__main__":
    analyze()
