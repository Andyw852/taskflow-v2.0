#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kl_fc_backends.py —— S5_fc 计算节点作业驱动：数据桥接 → 拟合 → 双格式导出 → 虚频闸。

设计：S5_fc 现在是【提交到计算节点】的作业（不再登录节点 gen 里裸跑）。submit.sh 依次调用
本文件的子命令，全部在 step5_fc/ 工作目录内执行：

  prep          收 step4_disp/disp-*/vasprun.xml → FORCES_FC3 → 从 phono3py_disp.yaml 建
                phono3py 对象；写 POSCAR(原胞)/SPOSCAR(超胞)；alm 分支再落 dataset_disps.npy
                / dataset_forces.npy（末帧=零平衡帧，供 pheasy 用）；拷 phono3py_disp.yaml
                到 phono3py/ 子目录，并（若有 step3_nac）生成 BORN。
  fit_phono3py  phono3py + symfc/alm 拟合 fc2/fc3（full）→ phono3py/{fc2.hdf5,fc3.hdf5,
                phono3py_params.yaml}。移植自 run_phono3py_fit.sh（去掉 npy 读入与
                VASP↔phonopy 原子重排：taskflow 的位移本就是 phonopy 超胞原子序）。
  collect_pheasy 把 pheasy CLI 产出的 fc2.hdf5/fc3.hdf5（在 cwd）搬进 phono3py/。
  post          可选 shengbte 导出（fc2/fc3 → hiphive → shengbte/FORCE_CONSTANTS_2ND/3RD）
                + 声子谱虚频闸（phonopy mesh 最小频率）→ step5_fc/phonon_summary.json。

拟合器/求解器由 step.conf 决定，gen_step5_fc.py 把解析结果写进 fit_config.json，本文件只读它。
产出布局（满足"任一拟合器都产出两套格式，放不同文件夹"）：
  step5_fc/phono3py/  fc2.hdf5 fc3.hdf5 phono3py_disp.yaml [phono3py_params.yaml] [BORN]
  step5_fc/shengbte/  FORCE_CONSTANTS_2ND FORCE_CONSTANTS_3RD POSCAR   (CONTROL 由 S6 按运行参数写)
  step5_fc/phonon_summary.json   ← S5 marker: '"stable": true'
"""
import glob
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

DISP_DIR   = "../step4_disp"
NAC_DIR    = "../step3_nac"
P3PY_SUB   = "phono3py"
SB_SUB     = "shengbte"


# ==========================================================================
# 小工具
# ==========================================================================
def load_cfg(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run(cmd, **kw):
    """前台跑命令，非零即抛。cmd 为字符串走 shell。"""
    print("[cmd] %s" % (cmd if isinstance(cmd, str) else " ".join(cmd)), flush=True)
    r = subprocess.run(cmd, shell=isinstance(cmd, str), **kw)
    if r.returncode != 0:
        sys.exit("[ERROR] 命令失败(rc=%d): %s" % (r.returncode, cmd))
    return r


def collect_vaspruns(disp_dir):
    """按编号排序收 disp-*/vasprun.xml。

    返回 (位移帧路径列表, 缺帧编号列表, 平衡帧路径或 None)。
    kleq：disp-00000 是平衡帧（未位移的完美超胞），单独摘出 —— 它不属于位移
    dataset，只用来扣残余力；混进位移列表会让帧序整体错位一格。
    """
    subs = sorted(glob.glob(str(Path(disp_dir) / "disp-*")),
                  key=lambda p: int(re.search(r"disp-(\d+)", p).group(1)))
    files, missing, eq = [], [], None
    for d in subs:
        num = re.search(r"disp-(\d+)", d).group(1)
        vr = Path(d) / "vasprun.xml"
        ok = vr.is_file() and vr.stat().st_size
        path = "%s/disp-%s/vasprun.xml" % (disp_dir, num)
        if int(num) == 0:
            eq = path if ok else None
        elif ok:
            files.append(path)
        else:
            missing.append(num)
    return files, missing, eq


def make_born(out):
    """有 step3_nac 的 vasprun 就用 phonopy-vasp-born 生成 out/BORN。返回是否成功。"""
    vr = Path(NAC_DIR) / "vasprun.xml"
    if not vr.is_file():
        print("[..] 无 step3_nac 结果 → 不加 NAC")
        return False
    run("phonopy-vasp-born %s > %s 2>born.log || true" % (str(vr), str(out / "BORN")))
    ok = (out / "BORN").is_file() and (out / "BORN").stat().st_size > 0
    print("[%s] BORN（NAC）%s" % ("OK" if ok else "WARN",
                                  "已生成" if ok else "生成失败，退回无 NAC"))
    if not ok:
        try:
            (out / "BORN").unlink()
        except OSError:
            pass
    return ok


# ==========================================================================
# prep：数据桥接（vasprun + phono3py_disp.yaml → ph 对象 / POSCAR / SPOSCAR / npy）
# ==========================================================================
def _read_forces(files, natom):
    """读一批 vasprun.xml 的力，返回 (n, natom, 3) 的 ndarray。"""
    import numpy as np
    from phonopy.interface.vasp import parse_set_of_forces
    d = parse_set_of_forces(natom, [str(f) for f in files], verbose=False)
    return np.asarray(d["forces"], float)


def _load_ph3_with_forces(disp_yaml):
    """load phono3py_disp.yaml（含位移 dataset）；phono3py.load 见 cwd 里有 FORCES_FC3
    会自动把力读进 dataset（日志里 'Displacement dataset for fc3 was read from FORCES_FC3'）。
    注意：不能用 parse_FORCES_FC3()——那是 type1(有限位移) 专用，要 dataset['natom']/
    'first_atoms'；alm 是 type2(随机位移) dataset，会 KeyError。"""
    import phono3py
    # kleq：prep 现在把（可能缺帧、已扣残余力的）dataset 存成 phono3py_params.yaml，
    #   它自带 forces，优先读它；没有才回落到 disp yaml + FORCES_FC3 的老路。
    _pm = Path(disp_yaml).parent / "phono3py_params.yaml"
    if _pm.is_file():
        _p = phono3py.load(str(_pm), produce_fc=False, is_nac=False, log_level=0)
        _d = _p.dataset or {}
        if (_d.get("forces") is not None
                or (_d.get("first_atoms") and "forces" in _d["first_atoms"][0])):
            print("[..] 力来自 %s（prep 已扣平衡帧残余力）" % _pm.name)
            return _p
    ph3 = phono3py.load(str(disp_yaml), produce_fc=False, is_nac=False, log_level=1)
    ds = ph3.dataset
    has_forces = (("forces" in ds and ds["forces"] is not None)
                  or ("first_atoms" in ds and ds.get("first_atoms")
                      and "forces" in ds["first_atoms"][0]))
    if not has_forces:
        # 兜底：个别版本 load 不自动读力时，显式指定 FORCES_FC3 重载一次（比手撕文本稳）。
        ph3 = phono3py.load(str(disp_yaml), forces_fc3_filename="FORCES_FC3",
                            produce_fc=False, is_nac=False, log_level=1)
        ds = ph3.dataset
        has_forces = "forces" in ds and ds["forces"] is not None
        print("[..] load 未自动带力，已用 forces_fc3_filename 重载 FORCES_FC3")
    if not has_forces:
        sys.exit("[ERROR] dataset 里没有 forces —— FORCES_FC3 未被读入。"
                 "确认 step5_fc/ 下已生成 FORCES_FC3（prep 的 --cf3 步）。")
    return ph3


def cmd_prep(cfg):
    from phonopy.interface.vasp import write_vasp
    import numpy as np

    out = Path.cwd()
    method = cfg["METHOD"]
    engine = cfg["FIT_ENGINE"]
    p3dir = out / P3PY_SUB
    p3dir.mkdir(exist_ok=True)

    disp_yaml = Path(DISP_DIR) / "phono3py_disp.yaml"
    if not disp_yaml.is_file():
        sys.exit("[ERROR] %s 不存在（step4 未生成位移）" % disp_yaml)
    shutil.copyfile(disp_yaml, out / "phono3py_disp.yaml")
    shutil.copyfile(disp_yaml, p3dir / "phono3py_disp.yaml")

    import numpy as np
    import phono3py

    files, missing, eq_file = collect_vaspruns(DISP_DIR)
    if not files:
        sys.exit("[ERROR] %s 下没有 disp-*/vasprun.xml，位移单点还没算完" % DISP_DIR)
    if not eq_file:
        sys.exit("[ERROR] 缺平衡帧 %s/disp-00000/vasprun.xml。\n"
                 "        它是未位移的完美超胞单点：拟合要用它扣残余力，也是缺帧容错的锚。\n"
                 "        补法：tf -tt kl-dft-cpu -p <材料> -j S4_disp retry（gen 幂等，只补出缺的\n"
                 "        disp-00000，已算完的位移帧不动），再 tf ... -j S4_disp start -f。"
                 % DISP_DIR)

    ph3 = phono3py.load(str(out / "phono3py_disp.yaml"),
                        produce_fc=False, is_nac=False, log_level=0)
    nsc = len(ph3.supercell)
    ds = ph3.dataset or {}
    # type-2（随机位移）dataset 有 displacements 数组；type-1（有限位移）是 first_atoms。
    is_rand = ds.get("displacements") is not None

    # kleq：平衡帧的残余力（未完全弛豫 / egg-box 都体现在这里），逐帧扣掉。
    f_eq = _read_forces([eq_file], nsc)[0]
    print("[..] 平衡帧 max|F_eq| = %.4f eV/Å（逐帧扣除）"
          % float(np.linalg.norm(f_eq, axis=1).max()))

    n_all = len(files) + len(missing)
    if missing:
        head = ",".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        if not is_rand:
            sys.exit("[ERROR] 有限位移法（METHOD=findiff）不能缺帧：每个对称不等价位移都要\n"
                     "        参与有限差分重建 fc2/fc3，少一帧就重建不出来。缺 %d 帧（disp-%s）。\n"
                     "        请补算后再来 S5；要容忍失败帧就把 step4 改成 METHOD=alm\n"
                     "        （随机位移 + 回归拟合，天然可以少几帧）。" % (len(missing), head))
        ratio = len(files) / float(max(n_all, 1))
        min_ratio = float(cfg.get("MIN_SUCCESS_RATIO", 0.9) or 0)
        min_frames = int(cfg.get("MIN_SUCCESS_FRAMES", 0) or 0)
        if ratio < min_ratio or len(files) < min_frames:
            sys.exit("[ERROR] 成功帧 %d/%d（%.0f%%）低于下限"
                     "（MIN_SUCCESS_RATIO=%s，MIN_SUCCESS_FRAMES=%s）。\n"
                     "        补算失败帧，或在 step5_fc 的 step.conf 里调低下限。"
                     % (len(files), n_all, ratio * 100, min_ratio, min_frames))
        print("[WARN] 缺 %d 帧（disp-%s）—— 随机位移法按 %d/%d（%.0f%%）继续拟合"
              % (len(missing), head, len(files), n_all, ratio * 100))
    print("[..] 收 %d 个 vasprun.xml（方法=%s，拟合器=%s）" % (len(files), method, engine))

    forces = _read_forces(files, nsc) - f_eq[None]
    if is_rand:
        disps = np.asarray(ds["displacements"], float)
        idx = [int(re.search(r"disp-(\d+)", f).group(1)) - 1 for f in files]
        if max(idx) >= len(disps):
            sys.exit("[ERROR] disp-%05d 超出 phono3py_disp.yaml 里的 %d 帧位移 —— "
                     "step4 的位移集与 disp-* 目录对不上，请 rerun S4_disp。"
                     % (max(idx) + 1, len(disps)))
        ph3.dataset = {"displacements": disps[idx], "forces": forces}
    else:
        # phono3py 4.x：findiff dataset 保留 included=False 的 second_atoms
        # （被 FC3_CUTOFF_PAIR 过滤掉的对位移，未生成 disp 目录、未算力），而
        # forces setter 遍历所有 second_atoms（不跳过 included=False）——需提供
        # 与 dataset 位移数一致的完整数组，included=False 填 0（fc3 拟合会跳过）。
        # 映射：disp 目录编号 == dataset 位移的 id（first_atoms[0].id=1，
        # second_atoms 的 id=2..N），故 forces_full[id-1] = 对应帧的力。
        n_total = 1 + sum(len(fa.get("second_atoms", []))
                          for fa in ph3.dataset.get("first_atoms", []))
        forces_full = np.zeros((n_total, nsc, 3), dtype=float)
        for f, fv in zip(files, forces):
            num = int(re.search(r"disp-(\d+)", f).group(1))
            forces_full[num - 1] = fv
        ph3.forces = forces_full
    # 存成自带力的 phono3py_params.yaml：后面的拟合直接读它，不再走
    #   `phono3py --cf3` + FORCES_FC3（那条路要求文件数==位移数，缺一帧就错位）。
    ph3.save(str(out / "phono3py_params.yaml"))
    print("[OK] phono3py_params.yaml：%d 帧力已写入（已扣平衡帧；另 %d 个 cutoff 外对填 0）"
          % (len(forces), n_total - len(forces)))

    ph3 = _load_ph3_with_forces(out / "phono3py_disp.yaml")
    write_vasp(str(out / "POSCAR"), ph3.unitcell, direct=True)     # 原胞
    write_vasp(str(out / "SPOSCAR"), ph3.supercell, direct=True)   # 理想超胞

    # pheasy 走随机位移路：落 dataset_disps.npy/dataset_forces.npy（末帧=零平衡帧）。
    #   taskflow 的位移已是"相对完美超胞的笛卡尔位移"，直接当 cartesian 喂 pheasy；
    #   追加一帧零位移/零力当"平衡参考"，pheasy 脚本会 (x - x[-1])[:-1] 还原成原样。
    if engine == "pheasy":
        if method != "alm":
            sys.exit("[ERROR] FIT_ENGINE=pheasy 需要随机位移（METHOD=alm）。"
                     "findiff 请用 FIT_ENGINE=phono3py。")
        ds = ph3.dataset
        if "displacements" not in ds or "forces" not in ds:
            sys.exit("[ERROR] 随机位移 dataset 缺 displacements/forces（type-2 期望）")
        disps = np.asarray(ds["displacements"], float)   # (n, Nsc, 3) 笛卡尔 Å
        forces = np.asarray(ds["forces"], float)         # (n, Nsc, 3) eV/Å
        nsc = disps.shape[1]
        zeros = np.zeros((1, nsc, 3))
        np.save(out / "dataset_disps.npy", np.concatenate([disps, zeros], axis=0))
        np.save(out / "dataset_forces.npy", np.concatenate([forces, zeros], axis=0))
        print("[OK] dataset_disps/forces.npy 就绪：%d 帧(+1 平衡) × %d 原子" % (len(disps), nsc))

    make_born(p3dir)
    print("[DONE] prep 完成")


# ==========================================================================
# fit_phono3py：phono3py + symfc/alm → full fc2/fc3 → phono3py/
# ==========================================================================
def cmd_fit_phono3py(cfg):
    from phono3py.file_IO import write_fc2_to_hdf5, write_fc3_to_hdf5

    out = Path.cwd()
    p3dir = out / P3PY_SUB
    calc = (cfg.get("FC_CALC") or "symfc").lower()
    cutoff = cfg.get("FC3_CUTOFF")
    ph3 = _load_ph3_with_forces(out / "phono3py_disp.yaml")

    print("[..] phono3py 拟合器=%s  fc3_cutoff=%s" % (calc, cutoff))
    # full fc（is_compact_fc=False）：既给 phono3py-load κ，也便于就地喂 hiphive 导 ShengBTE。
    ph3.produce_fc2(fc_calculator=calc, is_compact_fc=False)
    opts = None if cutoff in (None, "None", "none", "") else "cutoff = %s" % cutoff
    ph3.produce_fc3(fc_calculator=calc, fc_calculator_options=opts, is_compact_fc=False)

    write_fc2_to_hdf5(ph3.fc2, filename=str(p3dir / "fc2.hdf5"))   # full，无 p2s_map
    write_fc3_to_hdf5(ph3.fc3, filename=str(p3dir / "fc3.hdf5"))

    # NAC → phono3py_params.yaml（有 BORN 才写；供溯源/kappa 备选）
    born = p3dir / "BORN"
    if born.is_file():
        try:
            from phonopy.file_IO import parse_BORN
            nac = parse_BORN(ph3.primitive, filename=str(born))
            if isinstance(nac, dict) and not nac.get("factor"):
                nac["factor"] = 14.399652
            ph3.nac_params = nac
            print("[OK] BORN → nac_params")
        except Exception as e:
            print("[WARN] 读 BORN 失败，不写 nac_params：%s" % e)
    try:
        ph3.save(str(p3dir / "phono3py_params.yaml"))
    except Exception as e:
        print("[WARN] 写 phono3py_params.yaml 失败（不影响 kappa，用 phono3py_disp.yaml）：%s" % e)

    for f in ("fc2.hdf5", "fc3.hdf5"):
        if not (p3dir / f).is_file():
            sys.exit("[ERROR] phono3py 拟合未产出 %s" % f)

    # ShengBTE 导出：就地用内存里的 full 数组（和 run_phono3py_fit.sh 一致，最稳）
    if str(cfg.get("EXPORT_SHENGBTE", "true")).lower() in ("true", "1", "yes"):
        import numpy as np
        _export_shengbte(out, np.asarray(ph3.fc2), np.asarray(ph3.fc3))
    print("[DONE] fit_phono3py：phono3py/fc2.hdf5 + fc3.hdf5 就绪")


# ==========================================================================
# collect_pheasy：把 pheasy 的产物搬进 phono3py/
# ==========================================================================
def cmd_collect_pheasy(cfg):
    out = Path.cwd()
    p3dir = out / P3PY_SUB
    for f in ("fc2.hdf5", "fc3.hdf5"):
        src = out / f
        if not src.is_file():
            sys.exit("[ERROR] pheasy 没产出 %s（看 queue.err/out）" % f)
        shutil.move(str(src), str(p3dir / f))
    print("[..] collect_pheasy：fc2.hdf5 + fc3.hdf5 → phono3py/")

    # ShengBTE 导出：pheasy --full_ifc 产出 full fc，复读成数组喂 hiphive
    if str(cfg.get("EXPORT_SHENGBTE", "true")).lower() in ("true", "1", "yes"):
        try:
            fc2, fc3 = _read_full_fc(p3dir)
            _export_shengbte(out, fc2, fc3)
        except Exception as e:
            print("[WARN] pheasy fc 复读/导出 ShengBTE 失败（phono3py 路不受影响）：%s" % e)
    print("[DONE] collect_pheasy 完成")


# ==========================================================================
# post：shengbte 导出（可选） + 虚频闸 → phonon_summary.json
# ==========================================================================
def _read_full_fc(p3dir):
    """从 phono3py/{fc2.hdf5,fc3.hdf5} 复读 full fc 数组（供 hiphive）。"""
    import numpy as np
    from phono3py.file_IO import read_fc2_from_hdf5, read_fc3_from_hdf5
    fc2 = np.asarray(read_fc2_from_hdf5(filename=str(p3dir / "fc2.hdf5")))
    fc3 = np.asarray(read_fc3_from_hdf5(filename=str(p3dir / "fc3.hdf5")))
    return fc2, fc3


def _export_shengbte(out, fc2, fc3):
    """给定 full fc2/fc3 数组 → hiphive → shengbte/FORCE_CONSTANTS_2ND + _3RD + POSCAR。
    CONTROL 不在此写：它依赖 S6 的运行参数（T/mesh/scalebroad/NAC），由 S6 生成。"""
    import numpy as np
    from ase import Atoms
    from phonopy.interface.vasp import read_vasp
    try:
        from hiphive import ForceConstants
    except Exception as e:
        print("[WARN] 无 hiphive，跳过 ShengBTE 力常数导出（phono3py 侧不受影响）：%s" % e)
        return False

    sbdir = out / SB_SUB
    sbdir.mkdir(exist_ok=True)

    uc = read_vasp(str(out / "POSCAR"))
    sc = read_vasp(str(out / "SPOSCAR"))
    to_ase = lambda c: Atoms(numbers=c.numbers, scaled_positions=c.scaled_positions,
                             cell=c.cell, pbc=True)
    uc_ase, sc_ase = to_ase(uc), to_ase(sc)
    fc2 = np.asarray(fc2); fc3 = np.asarray(fc3)
    natom = len(sc_ase)

    try:
        if fc2.shape[0] != natom or fc3.shape[0] != natom:
            print("[WARN] fc 非 full（fc2%s fc3%s，超胞 %d 原子），ShengBTE 导出仅支持 full fc；"
                  "跳过。phono3py 路不受影响。" % (fc2.shape, fc3.shape, natom))
            return False
        fcs = ForceConstants.from_arrays(sc_ase, fc2_array=fc2, fc3_array=fc3)
    except Exception as e:
        print("[WARN] 构造 hiphive ForceConstants 失败，跳过 ShengBTE 导出：%s" % e)
        return False

    # hiphive write_to_shengBTE 内部要求原胞分数坐标严格 <1，抹掉 0.9999.../1e-10 噪声
    def _wrap(a, eps=1e-9):
        fr = a.get_scaled_positions(wrap=True)
        fr = np.where(fr >= 1.0 - eps, 0.0, fr)
        fr = np.where(fr < eps, 0.0, fr)
        a.set_scaled_positions(fr)
    _wrap(uc_ase)
    try:
        _wrap(fcs._supercell)
    except Exception:
        pass

    fcs.write_to_phonopy(str(sbdir / "FORCE_CONSTANTS_2ND"), format="text")
    fcs.write_to_shengBTE(str(sbdir / "FORCE_CONSTANTS_3RD"), uc_ase)
    from ase.io import write as ase_write
    ase_write(str(sbdir / "POSCAR"), uc_ase, format="vasp", direct=True, sort=False)
    ok = (sbdir / "FORCE_CONSTANTS_2ND").is_file() and (sbdir / "FORCE_CONSTANTS_3RD").is_file()
    print("[%s] ShengBTE 力常数导出%s（CONTROL 由 S6 按运行参数写）"
          % ("OK" if ok else "WARN", "完成" if ok else "失败"))
    return ok


def _parse_min_freq(band_yaml):
    if not Path(band_yaml).is_file():
        return None
    fr = []
    for ln in Path(band_yaml).read_text(errors="ignore").splitlines():
        m = re.match(r"\s*frequency:\s*([-+]?\d*\.?\d+(?:[eEdD][-+]?\d+)?)\s*$", ln)
        if not m:
            continue
        try:
            fr.append(float(m.group(1).replace("d", "e").replace("D", "e")))
        except ValueError:
            continue
    return min(fr) if fr else None


def _stability_gate(cfg, out):
    # phonopy 判虚频（q-mesh 最小频率）。2D 材料默认用【无 NAC】判据：3D 库仑核的 NAC 在
    # 严格 2D 体系近 Γ 会产生【虚假虚频】(LO-TO 在真 2D 应趋零)，与 step6 KAPPA_NAC=auto 一致。
    # NAC 与无 NAC 两个最小频率都算出来记进 summary 供对照。返回诊断 dict。
    import numpy as np
    p3dir = out / P3PY_SUB
    dim = str(cfg.get("DIM", "")).lower()
    is2d = dim.startswith("2")

    def _err(msg):
        return {"tool_ok": False, "stable": False, "min_freq": None,
                "min_freq_nonac": None, "min_freq_nac": None, "nac_used": False,
                "is_2d": is2d, "status": "tool_error", "note": msg}

    try:
        import phono3py
        from phono3py.file_IO import read_fc2_from_hdf5
        ph3 = phono3py.load(str(p3dir / "phono3py_disp.yaml"),
                            produce_fc=False, is_nac=False, log_level=0)
        uc, scm, pm = ph3.unitcell, ph3.supercell_matrix, ph3.primitive_matrix
        fc2 = np.asarray(read_fc2_from_hdf5(filename=str(p3dir / "fc2.hdf5")))
    except Exception as e:
        return _err("建 Phonopy / 读 fc2 失败：%s" % e)

    def _min_freq(with_nac):
        from phonopy import Phonopy
        ph = Phonopy(uc, supercell_matrix=scm, primitive_matrix=pm)
        ph.force_constants = fc2
        if with_nac:
            from phonopy.file_IO import parse_BORN
            nac = parse_BORN(ph.primitive, filename=str(p3dir / "BORN"))
            if isinstance(nac, dict) and not nac.get("factor"):
                nac["factor"] = 14.399652
            ph.nac_params = nac
        else:
            ph.nac_params = None          # 显式建"无 NAC"的动力学矩阵
        ph.run_mesh(mesh=60.0, with_eigenvectors=False, is_mesh_symmetry=True)
        return float(np.min(ph.get_mesh_dict()["frequencies"])), ph

    try:
        mf_nonac, ph_nonac = _min_freq(False)
    except Exception as e:
        return _err("phonopy mesh(no-NAC) 计算失败：%s" % e)

    mf_nac = None
    if (p3dir / "BORN").is_file():
        try:
            mf_nac, _ = _min_freq(True)
        except Exception as e:
            print("[WARN] 带 NAC 的 mesh 失败，仅用无 NAC 判据：%s" % e)

    # best-effort 出图（用无 NAC 版，避免 2D 的 NAC 假象污染谱图）；其最小值并入无 NAC 判据
    try:
        ph_nonac.auto_band_structure(plot=False, write_yaml=True, filename=str(p3dir / "band-dft-cpu.yaml"))
        bf = _parse_min_freq(p3dir / "band-dft-cpu.yaml")
        if bf is not None:
            mf_nonac = min(mf_nonac, bf)
    except Exception as e:
        print("[..] band-dft-cpu.yaml 出图跳过（不影响判据）：%s" % e)

    # 判据：2D 或拿不到 NAC → 用无 NAC；3D 有 NAC → 用 NAC
    if is2d or mf_nac is None:
        mf_used, nac_used = mf_nonac, False
    else:
        mf_used, nac_used = mf_nac, True

    thr = float(cfg.get("IMAG_THR", 0.10))
    stable = mf_used >= -thr
    parts = ["min_freq(no-NAC)=%.3f" % mf_nonac]
    if mf_nac is not None:
        parts.append("min_freq(NAC)=%.3f" % mf_nac)
    parts.append("判据用%s=%.3f THz(阈值 -%.2f)" % ("无NAC" if not nac_used else "NAC", mf_used, thr))
    note = "；".join(parts) + " → " + (
        "无明显虚频，稳定" if stable else "存在虚频(imaginary frequency)，动力学不稳定")
    return {"tool_ok": True, "stable": stable, "min_freq": mf_used,
            "min_freq_nonac": mf_nonac, "min_freq_nac": mf_nac, "nac_used": nac_used,
            "is_2d": is2d, "status": "stable" if stable else "imaginary", "note": note}


def cmd_post(cfg):
    out = Path.cwd()
    # ShengBTE 导出已在 fit_phono3py / collect_pheasy 就地完成；这里只查状态。
    want_sb = str(cfg.get("EXPORT_SHENGBTE", "true")).lower() in ("true", "1", "yes")
    sb_ok = (out / SB_SUB / "FORCE_CONSTANTS_3RD").is_file() if want_sb else None
    if want_sb and not sb_ok:
        print("[WARN] EXPORT_SHENGBTE=true 但未生成 shengbte/FORCE_CONSTANTS_3RD"
              "（多半是 hiphive 缺失或 fc 非 full）。SOLVER=shengbte 时 S6 会报缺文件。")

    g = _stability_gate(cfg, out)
    stable, tool_ok = g["stable"], g["tool_ok"]
    print("[%s] %s" % ("OK" if stable else "FAIL", g["note"]))

    (out / "phonon_summary.json").write_text(json.dumps(
        {"stable": bool(stable), "status": g["status"],
         "imaginary_frequency": bool(tool_ok and not stable),
         "min_frequency_THz": g["min_freq"],
         "min_freq_nonac_THz": g["min_freq_nonac"],
         "min_freq_nac_THz": g["min_freq_nac"],
         "nac_used_for_verdict": g["nac_used"], "is_2d": g["is_2d"],
         "fit_engine": cfg.get("FIT_ENGINE"), "fc_calc": cfg.get("FC_CALC"),
         "pheasy_method": cfg.get("PHEASY_FIT_METHOD"),
         "shengbte_export": sb_ok, "tool_ok": tool_ok, "note": g["note"]},
        ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] post：phonon_summary.json 就绪，status=%s stable=%s"
          % (g["status"], str(stable).lower()))
    # 区分两种"不通过"：
    #   工具错误(tool_ok=False，mesh 没算成) → 作业失败退出，tf 标 error，提醒去查日志；
    #   真有虚频(status=imaginary) → 正常退出，marker 不满足，S6 被合理挡住(非 error)。
    if not tool_ok:
        sys.exit("[ERROR] 虚频闸工具错误，作业按失败结束（见上 note / phono3py/band-dft-cpu.log）")


# ==========================================================================
CMDS = {"prep": cmd_prep, "fit_phono3py": cmd_fit_phono3py,
        "collect_pheasy": cmd_collect_pheasy, "post": cmd_post}


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in CMDS:
        sys.exit("用法：kl_fc_backends.py <%s> fit_config.json" % "|".join(CMDS))
    CMDS[sys.argv[1]](load_cfg(sys.argv[2]))


if __name__ == "__main__":
    main()