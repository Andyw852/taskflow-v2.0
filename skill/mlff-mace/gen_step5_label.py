#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step5_label.py —— mlff-mace step5：DFT 单点标注（fanout cfg-*，唯一昂贵一步）。

运行位置：超算登录节点，cwd = 材料目录；为每个待标注构型造一个 cfg-* 子目录
（POSCAR + INCAR + KPOINTS + POTCAR + submit.sh + MAGMOM），tf 自动按 fanout 提交。

操作定则（必须遵守）：
    ★ 只用 retry 或 start -f，绝不用 rerun / clean —— 后两者会 rm -rf 步骤目录，
      毁掉已经算完的 DFT 帧。retry 会先 scancel 在跑的作业再重跑 gen（gen 幂等，
      不删文件、不碰已算完的 cfg-*）；单独补某几帧就进对应 cfg-* 目录手工 sbatch。

DFT 设置（§8.1，从 step1 输出自动推导）：
    PREC=Accurate；ENCUT=ceil(1.5×ENMAX)（ENCUT_OVERRIDE 可覆盖）
    ISMEAR/SIGMA：step1 EIGENVAL 带隙 > 0.1 eV → 0/0.05，否则 1/0.2
    ISPIN/MAGMOM：step1 末次磁矩 max|m| > 0.1 μB → 2 + 逐原子继承（超胞按
                  image-major 展开，磁性体系绝不许用 VASP 默认初值）
    EDIFF=1E-7、LREAL=.FALSE.、ALGO=Normal、NELM=200、ISYM=0、IBRION=-1、NSW=0、
    LWAVE/LCHARG=.FALSE.、LASPH=.TRUE.、LMAXMIX 按元素（d→4 f→6 否则 2）、
    不许出现 MAXMIX；KPAR 从 IBZKPT 推（Γ-only 强制 1）、NCORE 默认 4；
    U 沿用 step1 的 LDAU 设置（与 relax 完全一致，U_OVERRIDE 计入指纹）
    KPOINTS：默认 Γ-only（超胞取力标准做法），KPOINTS_GRID 可显式覆盖
    12 核：step.conf [submit] nodes=1 ntasks_per_node=12

幂等：已算完（OUTCAR 完整）的 cfg-* 目录原样跳过；没算完的重建输入文件、不清 OUTCAR。
退出码 0 成功；非 0 = [ERROR]。
"""
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dft_settings as ds
import mlff_common as mc
import stepconf

OUTDIR = "step5_label"
STEP = "step5_label"
STEP4 = "step4_genstruct"


def outcar_done(path):
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with open(p, "rb") as fh:
            try:
                fh.seek(-200000, 2)
            except OSError:
                fh.seek(0)
            tail = fh.read().decode("utf-8", "ignore")
    except OSError:
        return False
    return "General timing and accounting informations" in tail


def write_incar(out, vals, comment_lines=()):
    """把字典写成 INCAR（保证标签顺序稳定、无 MAXMIX）。"""
    order = ["SYSTEM", "PREC", "ENCUT", "GGA", "IVDW", "ISMEAR", "SIGMA",
             "ISPIN", "MAGMOM", "LREAL", "LASPH", "LMAXMIX", "EDIFF", "NELM",
             "ALGO", "ISYM", "IBRION", "NSW", "LWAVE", "LCHARG", "KPAR",
             "NCORE", "LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ"]
    lines = ["# INCAR 由 gen_step5_label.py 按 step1 输出自动生成（勿手改，改 step.conf）"]
    lines += ["# " + c for c in comment_lines]
    for k in order:
        if k in vals and vals[k] not in (None, ""):
            lines.append("%-8s = %s" % (k, vals[k]))
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_kpoints(out, grid):
    if grid in (None, ""):
        lines = ["Gamma-only（mlff-mace 超胞取力标准做法；KPOINTS_GRID 可覆盖）",
                 "0", "Gamma", "1 1 1"]
    else:
        g = [int(x) for x in str(grid).split()]
        if len(g) != 3:
            sys.exit("[ERROR] KPOINTS_GRID 要三个整数，收到 %r" % grid)
        lines = ["Automatic mesh（KPOINTS_GRID=%s）" % grid,
                 "0", "Gamma", " ".join(map(str, g))]
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def poscar_elements(poscar_text):
    """POSCAR 的元素段序列（符号行 + 数量行，都在 Direct/Cartesian 之前）。
    返回 (symbols, counts)。

    VASP POSCAR 布局：0 注释 / 1 缩放 / 2-4 晶格 / 5 符号行 / 6 数量行 /
    7 (Selective dynamics | Direct | Cartesian)。mlff 自写盘恒为：5 符号、
    6 数量、7 Direct；但 step1 CONTCAR 由 VASP 写，可能带 Selective dynamics
    （在数量行之后）。只认两种模式：
      A. 第 5 行是元素符号（首 token 像 "C"/"Ba"/"Ba_sv"）→ 符号行=5，数量行=6
      B. 第 5 行直接是数量（无符号行变体，少见）→ 符号行=[]，数量行=5
    折行：VASP 符号/数量行不折行（每行 ≤ 10 种元素的限制对 mlff 不触发），
    但保险起见把 Direct 前的所有行按 token 归类。"""
    lines = poscar_text.splitlines()
    body = []
    for ln in lines[5:]:
        s = ln.strip()
        if not s:
            continue
        if s.lower().startswith(("direct", "cartesian", "selective")):
            break
        body.append(s)
    if not body:
        sys.exit("[ERROR] POSCAR 解析不出元素区：\n%s" % poscar_text[:300])
    syms, cnts = [], []
    for s in body:
        toks = s.split()
        if all(t.isdigit() for t in toks):
            cnts += [int(t) for t in toks]
        else:
            syms += toks
    if not cnts:
        sys.exit("[ERROR] POSCAR 解析不出数量行：\n%s" % poscar_text[:300])
    return syms, cnts


def build_potcar_for_frame(frame_potcar_syms, step1_potcar_text):
    """按该帧 POSCAR 的元素列表（有序、可能缺某元素如 iso 帧）从 step1 的完整
    POTCAR 里重拼一份：切出每个元素的段，按 POSCAR 段序拼接。

    POTCAR 每段以一行 "  PAW_PBE <sym> ..." 开头（真实文件段首如
    "  PAW_PBE C 08Apr2002"）。切分后把
    各段头部 TITEL 里的元素名（第二个空白 token，去掉 _sv/_pv/_d 后缀）作为键。
    找不到某元素 → [ERROR]（比让 VASP 悄悄用错势好）。"""
    lines = step1_potcar_text.splitlines(keepends=True)
    starts = []
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("PAW_PBE "):
            tok = s.split()[1]
            base = tok.split("_")[0]
            starts.append((idx, base, tok))
    if not starts:
        sys.exit("[ERROR] step1 POTCAR 解析不出 PAW_PBE 段首行")
    segs = {}
    for k, (idx, base, tok) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        segs.setdefault(base, []).append("".join(lines[idx:end]))
    out = []
    for sym in frame_potcar_syms:
        base = sym.split("_")[0]
        if base not in segs:
            sys.exit("[ERROR] 该帧需要元素 %s，但 step1 POTCAR 里只有 %s —— "
                     "POTCAR 与结构元素不一致（基座模型元素覆盖检查应拦住）"
                     % (sym, sorted(segs)))
        out.append(segs[base][0])
    return "".join(out)


def potcar_element_syms(potcar_text):
    """从 POTCAR 文本提取各段元素（TITEL 行第二 token，去 _sv/_pv/_d 后缀）。"""
    out = []
    for ln in potcar_text.splitlines():
        if "TITEL" in ln and "PAW_PBE" in ln:
            tok = ln.split("PAW_PBE")[1].strip().split()[0]
            out.append(tok.split("_")[0])
    return out


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)
    cv = dict(conf.params)

    # ---- 上游接力（§13：显式检查，绝不拿旧文件默默往下算）----
    man_path = cwd / STEP4 / "struct_manifest.json"
    if not man_path.is_file():
        sys.exit("[ERROR] %s 不存在 —— 先让 S4 生成结构。" % man_path)
    for need in ("step1_relax/OUTCAR", "step1_relax/INCAR", "step1_relax/POTCAR",
                 "step1_relax/EIGENVAL", "step1_relax/KPOINTS"):
        if not (cwd / need).is_file():
            sys.exit("[ERROR] 缺 %s —— step1_relax 没算完？" % need)
    man = json.loads(man_path.read_text())
    gen = man["generation"]

    # ---- DFT 设置（从 step1 输出推导）----
    incar1 = ds.read_incar(cwd / "step1_relax" / "INCAR")
    encut, encut_note = ds.encut_from_potcar(cwd / "step1_relax" / "POTCAR", 1.5,
                                             cv["ENCUT_OVERRIDE"])
    gap, gap_note = ds.read_bandgap(cwd / "step1_relax" / "EIGENVAL")
    ismear, sigma, sm_note = ds.decide_ismear(gap, gap_note)
    moments, m_note = ds.read_magmom(cwd / "step1_relax" / "OUTCAR")
    ispin, ispin_note = ds.magnetic_setting(moments)
    lmaxmix, lmm_note = ds.decide_lmaxmix(man["elements"])
    method = mc.read_kv(cwd / "step1_relax" / mc.METHOD_FILE)
    func = method.get("FUNC", "?")
    print("[..] ENCUT=%d（%s）  ISMEAR=%s SIGMA=%s（%s）  ISPIN=%s（%s）  LMAXMIX=%d"
          % (encut, encut_note, ismear, sigma, sm_note, ispin, ispin_note, lmaxmix))

    # [FIX-F2] 金属 + Γ-only 守卫：ISMEAR=1 说明 step1 判定为金属/半金属。
    # 金属的费米面对 k 点采样极敏感，~100 原子超胞只取 Γ 点会让力带上系统误差，
    # 而这个误差会原样进 fc2/fc3。半导体 Γ-only 没问题，金属必须显式给网格。
    if str(ismear).strip() == "1" and not (cv["KPOINTS_GRID"] or "").strip():
        sys.exit(
            "[ERROR] step1 判定本体系为金属（%s），但 KPOINTS_GRID 为空 = Γ-only。\n"
            "        金属超胞单点只取 Γ 点，力会有系统误差并原样进 fc2/fc3。\n"
            "        请显式设网格再重跑，例如：\n"
            "          tf -tt mlff-mace -p <材料> -j 5 conf --set params.KPOINTS_GRID='2 2 2'\n"
            "        （2D 体系真空方向恒为 1，如 '2 2 1'）\n"
            "        确认要用 Γ-only 就把 KPOINTS_GRID 显式写成 '1 1 1'。" % sm_note)

    # LDAU 从 step1 INCAR 原样继承（与弛豫完全一致）
    ldau = {k: incar1[k] for k in
            ("LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ") if k in incar1}
    u_note = ("LDAU=%s LDAUU=%s" % (ldau.get("LDAU", ".FALSE."),
                                    ldau.get("LDAUU", ""))) if ldau else "off"

    base = {"PREC": "Accurate", "ENCUT": str(encut),
            "ISMEAR": ismear, "SIGMA": sigma,
            "ISPIN": ispin, "LREAL": ".FALSE.", "LASPH": ".TRUE.",
            "LMAXMIX": str(lmaxmix), "EDIFF": "%g" % cv["EDIFF"],
            "NELM": "200", "ALGO": str(cv["ALGO"] or "Normal"),
            "ISYM": "0", "IBRION": "-1", "NSW": "0",
            "LWAVE": ".FALSE.", "LCHARG": ".FALSE.",
            "NCORE": str(cv["NCORE"])}
    base.update(ldau)
    base["GGA"] = incar1.get("GGA", "")
    base["IVDW"] = incar1.get("IVDW", "")

    # ---- POTCAR：不整段复制 step1，按每帧 POSCAR 的元素列表重拼 ----
    # [FIX-Ba1C20] 此前把所有 cfg 都写成 step1 完整 POTCAR（多元素体系 = 多段），
    # iso 帧（单元素孤立原子）的 POSCAR 只有一段，VASP 顺序取第一段 → 孤立 Ba 被
    # 当 C 算（OUTCAR VRHFIN=C、NELECT=4.0），能量静默错——单元素 Si 无感。
    # 现在按帧元素列表拼（iso 帧只含自己的元素），并断言与 POSCAR 段序一致。
    step1_potcar = (cwd / "step1_relax" / "POTCAR").read_text(errors="ignore")

    here = Path(__file__).resolve().parent
    n_new, n_done = 0, 0
    for ent in man["frames"]:
        cfgdir = out / ent["id"]
        poscar_src = cwd / STEP4 / ("gen-%d" % ent["gen"]) / ent["file"]
        if not poscar_src.is_file():
            sys.exit("[ERROR] 清单里的结构文件不存在：%s" % poscar_src)
        # 结构变更守卫：同一 cfg id 换过结构（如改 REF_DISP）时，旧 OUTCAR 是脏的，
        # 必须清掉重算（标签错了比多算一帧贵得多）
        import hashlib as _hl
        md5 = _hl.md5(poscar_src.read_bytes()).hexdigest()
        stamp = cfgdir / ".poscar_md5"
        old_poscar = cfgdir / "POSCAR"
        stale = (old_poscar.is_file() and
                 _hl.md5(old_poscar.read_bytes()).hexdigest() != md5)
        if outcar_done(cfgdir / "OUTCAR") and not stale:
            n_done += 1
            continue
        if stale:
            for _f in cfgdir.glob("*"):
                if _f.is_file() and _f.name not in ("submit.sh",):
                    _f.unlink()
            print("[..] %s 结构已变（REF_DISP/应变改了？），旧产物已清，重算" % ent["id"])
        cfgdir.mkdir(exist_ok=True)
        shutil.copyfile(str(poscar_src), str(cfgdir / "POSCAR"))
        stamp.write_text(md5, encoding="utf-8")
        # [FIX-Ba1C20] 断言：POSCAR 的元素段序列（数量行>0 的段）与后续重拼的
        # POTCAR 段序一致。这同时堵死两个静默坑：
        #   ① 超胞 POSCAR 符号行被写成 18 段交替（C Ba C Ba …）——元素段 ≠
        #      POTCAR 段数，VASP 直接 ERROR（S4 未 atom-major 排序时发生）；
        #   ② iso 帧 POTCAR 没按元素裁剪——元素段对不上，VASP 静默用错势。
        _poscar_text = (cfgdir / "POSCAR").read_text(encoding="utf-8")
        frame_syms, frame_cnts = poscar_elements(_poscar_text)
        if len(frame_syms) != len(frame_cnts):
            sys.exit("[ERROR] %s POSCAR 元素段数与数量行不一致（%d vs %d）——"
                     "S4 生成的符号行有问题？\n%s"
                     % (ent["id"], len(frame_syms), len(frame_cnts),
                        "\n".join(_poscar_text.splitlines()[:8])))
        if len(frame_syms) > 1 and any(
                frame_syms[i] == frame_syms[i + 1] for i in range(len(frame_syms) - 1)):
            sys.exit("[ERROR] %s POSCAR 元素段重复相邻（%s）——同元素被拆成多段，"
                     "S4 应把同元素聚拢再写盘" % (ent["id"], frame_syms))
        # MAGMOM（磁性体系逐原子继承；iso 帧有自己的）
        mag_file = cwd / STEP4 / ("gen-%d" % ent["gen"]) / \
            ent["file"].replace(".poscar", ".magmom")
        vals = conf.apply_incar({}, base)   # [incar]→自动推导→[incar.final]→删除
        if mag_file.is_file():
            vals["MAGMOM"] = mag_file.read_text().split("=", 1)[1].strip()
        comment = ["%s  %s  gen=%d  strain_factor=%s  volume_factor=%s  rattle_std=%s"
                   % (ent["id"], ent["config_type"], ent["gen"],
                      ent.get("strain_factor"), ent.get("volume_factor"),
                      ent.get("rattle_std")),
                   "DFT 设置来源：step1 输出推导（FUNC=%s，%s）" % (func, u_note),
                   "ENCUT=%d（%s）" % (encut, encut_note)]
        write_incar(cfgdir / "INCAR", vals, comment)
        if ent["config_type"] == "iso":
            write_kpoints(cfgdir / "KPOINTS", None)      # 孤立原子 Γ-only
            vals2 = dict(vals)
            vals2["KPAR"] = "1"
        else:
            write_kpoints(cfgdir / "KPOINTS", cv["KPOINTS_GRID"])
            # KPAR：Γ-only 强制 1；显式网格按 IBZKPT 逻辑简化为 1（12 核小作业）
            vals2 = dict(vals)
            vals2["KPAR"] = "1"
        # KPAR/NCORE 写进 INCAR（补在末尾，稳定顺序）
        incar_lines = (cfgdir / "INCAR").read_text().splitlines()
        incar_lines += ["KPAR    = %s   # 12核：从 IBZKPT 推（Γ-only 强制 1）" % vals2["KPAR"]]
        (cfgdir / "INCAR").write_text("\n".join(incar_lines) + "\n",
                                      encoding="utf-8", newline="\n")
        # [FIX-Ba1C20] 按该帧 POSCAR 元素列表重拼 POTCAR（iso 帧只有自己的元素段）
        potcar_text = build_potcar_for_frame(frame_syms, step1_potcar)
        (cfgdir / "POTCAR").write_text(potcar_text, encoding="utf-8")
        # 断言：拼出的 POTCAR 元素序 == POSCAR 元素段序（防切段 bug 静默错势）
        _pot_syms = potcar_element_syms(
            (cfgdir / "POTCAR").read_text(encoding="utf-8"))
        if _pot_syms != frame_syms:
            sys.exit("[ERROR] %s 重拼 POTCAR 元素序 %s ≠ POSCAR %s——切段有 bug"
                     % (ent["id"], _pot_syms, frame_syms))

        # ---- submit.sh（12 核来自 step.conf [submit]）----
        dim = man["dim"]
        tpl = mc.resolve_submit(here, "submit_std", dim)
        mc.write_submit(tpl, cfgdir / "submit.sh",
                        {"JOBNAME": mc.new_jobname(cwd, ent["id"].replace("cfg-", "l"),
                                                   tag="mlff")[:80]})
        stepconf.apply_submit(cfgdir / "submit.sh", conf.submit)
        n_new += 1
    print("[DONE] %s：本代 %d 帧，已算完 %d，新生成 %d 个 cfg 目录（12 核提交）"
          % (OUTDIR, man["n_frames"], n_done, n_new))


if __name__ == "__main__":
    main()
