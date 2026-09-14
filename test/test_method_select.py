# -*- coding: utf-8 -*-
"""method_select 共享模块单元测试（纯标准库 + mock，无需本机 pymatgen）。

覆盖：
  · read_incar / standardize_incar_value 类型标准化、注释/分号语法
  · potcar_sha1 流式算法与确定性
  · sniff_func_from_tags 泛函反推
  · default_method 决策表（参照/吸附、异常保守、低维、多组分、等维单组分 -> pbesol）
    且"等维"分支的 reason 不宣称等维=纯共价
  · analyze_bond_network 在无 pymatgen 时优雅降级 ok=False
  · make_method_card / write_method_card / 卡指纹（改身份字段必变指纹）
  · assert_method_consistent：默认不一致报错；只接受非空理由豁免并记录
    （返回 waivers + 落盘 method_card_waivers.jsonl）；篡改卡指纹被识破
  · validate_physical_constraints：PS+非零IVDW缺VDW_S8/A1/A2；低维 ISIF=3 无约束

运行：
  cd ~/software/taskflow-v2.0 && python3 test/test_method_select.py     # 独立运行器
  cd ~/software/taskflow-v2.0 && python3 -m pytest test/test_method_select.py -v  # 若有 pytest
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "skill", "_common", "opt"))
os.chdir(_ROOT)

import method_select as M  # noqa: E402


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _mk_incar_dir(lines, potcar_bytes=None):
    d = tempfile.mkdtemp(prefix="ms_test_")
    with open(os.path.join(d, "INCAR"), "w", encoding="utf-8") as fh:
        fh.write(lines if lines.endswith("\n") else lines + "\n")
    if potcar_bytes is not None:
        with open(os.path.join(d, "POTCAR"), "wb") as fh:
            fh.write(potcar_bytes)
    return d


def _rm(d):
    shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------
# read_incar / 类型标准化
# ----------------------------------------------------------------------
def test_standardize_incar_value_types():
    assert M.standardize_incar_value(".TRUE.") is True
    assert M.standardize_incar_value(".true.") is True
    assert M.standardize_incar_value("T") is True
    assert M.standardize_incar_value("FALSE") is False
    assert M.standardize_incar_value(".FALSE.") is False
    assert M.standardize_incar_value("500") == 500
    assert isinstance(M.standardize_incar_value("500"), int)
    assert M.standardize_incar_value("0.2") == 0.2
    assert M.standardize_incar_value("1E-6") == 1e-6
    assert M.standardize_incar_value("Accurate") == "Accurate"
    assert M.standardize_incar_value("'quoted'") == "quoted"
    assert M.standardize_incar_value('"dq"') == "dq"
    assert M.standardize_incar_value("  Normal  ") == "Normal"


def test_read_incar_typed_and_comments():
    d = _mk_incar_dir(
        "SYSTEM = demo\n"
        "GGA = PE   # 泛函\n"
        "IVDW = 12 ! D3(BJ)\n"
        "ENCUT = 500\n"
        "PREC = Accurate\n"
        "LASPH = .TRUE.\n"
        "ISMEAR = 1; SIGMA = 0.2\n"
        "# 整行注释\n"
        "! 另一条整行注释\n"
    )
    try:
        v = M.read_incar(d)
        assert v["SYSTEM"] == "demo"
        assert v["GGA"] == "PE"
        assert v["IVDW"] == 12
        assert v["ENCUT"] == 500
        assert v["PREC"] == "Accurate"
        assert v["LASPH"] is True
        assert v["ISMEAR"] == 1
        assert v["SIGMA"] == 0.2
    finally:
        _rm(d)


def test_read_incar_accepts_file_or_dir_and_empty():
    d = _mk_incar_dir("A = 1\n")
    try:
        assert M.read_incar(os.path.join(d, "INCAR")) == {"A": 1}
        e = tempfile.mkdtemp()
        try:
            assert M.read_incar(e) == {}
        finally:
            _rm(e)
    finally:
        _rm(d)


# ----------------------------------------------------------------------
# POTCAR SHA1（流式）
# ----------------------------------------------------------------------
def test_potcar_sha1_matches_hashlib_and_streams():
    d = _mk_incar_dir("GGA = PE\n", potcar_bytes=b"X" * 300_000)
    try:
        p = os.path.join(d, "POTCAR")
        with open(p, "rb") as fh:
            expected = hashlib.sha1(fh.read()).hexdigest()
        assert M.potcar_sha1(p) == expected
        # 小 chunk 也要与整文件一致（证明是流式、不是整读）
        assert M.potcar_sha1(p, chunk_size=7) == expected
        assert M.potcar_sha1(os.path.join(d, "NOPE")) is None
    finally:
        _rm(d)


# ----------------------------------------------------------------------
# sniff_func_from_tags
# ----------------------------------------------------------------------
def test_sniff_func_from_tags():
    assert M.sniff_func_from_tags({"GGA": "PE", "IVDW": 12}) == "pbe-d3"
    assert M.sniff_func_from_tags({"GGA": "PS"}) == "pbesol"
    assert M.sniff_func_from_tags({"GGA": "PE"}) == "pbe"
    assert M.sniff_func_from_tags({"GGA": "PE", "IVDW": 0}) == "pbe"
    assert M.sniff_func_from_tags({"GGA": "PW91"}) is None


# ----------------------------------------------------------------------
# default_method 决策表
# ----------------------------------------------------------------------
def _fake_analysis(bond_dim, n_components):
    M._set_analyze_hook(lambda structure: {
        "ok": True, "bond_dim": bond_dim,
        "n_components": n_components,
        "note": "fake"})
    try:
        return lambda *a, **kw: M.default_method(*a, **kw)
    finally:
        M._set_analyze_hook(None)


def test_default_method_adsorbate_and_molecular_reference_prefer_d3():
    for kwargs in ({"has_adsorbate": True},
                   {"uses_molecular_reference": True}):
        func, reason = M.default_method("fake", "3d", **kwargs)
        assert func == "pbe-d3"
        assert reason  # 非空理由
    func, reason = M.default_method("fake", "2d",
                                    has_adsorbate=True,
                                    uses_molecular_reference=True)
    assert func == "pbe-d3"


def test_default_method_structure_none_conservative():
    func, reason = M.default_method(None, "3d")
    assert func == "pbe-d3"
    assert "保守" in reason


def test_default_method_no_pymatgen_conservative():
    # 本机没装 pymatgen：真实分析路径返回 ok=False，应走"异常保守"
    func, reason = M.default_method("dummy-structure", "3d")
    assert func == "pbe-d3"
    assert "保守" in reason


def test_default_method_bond_lt_struct_dim_uses_d3():
    M._set_analyze_hook(lambda s: {"ok": True, "bond_dim": 2,
                                   "n_components": 1, "note": "fake"})
    try:
        func, reason = M.default_method("s", "3d")
        assert func == "pbe-d3"
        assert "低于结构维度" in reason
    finally:
        M._set_analyze_hook(None)


def test_default_method_multi_component_uses_d3():
    M._set_analyze_hook(lambda s: {"ok": True, "bond_dim": 3,
                                   "n_components": 2, "note": "fake"})
    try:
        func, reason = M.default_method("s", "3d")
        assert func == "pbe-d3"
        assert "独立组分" in reason
    finally:
        M._set_analyze_hook(None)


def test_default_method_equal_dim_single_component_pbesol_hedged():
    M._set_analyze_hook(lambda s: {"ok": True, "bond_dim": 3,
                                   "n_components": 1, "note": "fake"})
    try:
        func, reason = M.default_method("s", "3d")
        assert func == "pbesol"
        # ★ 措辞约束：不能宣称"等维 => 纯共价"
        assert "并不证明体系是纯共价" in reason
    finally:
        M._set_analyze_hook(None)
    # 2D 单层单组分等维
    M._set_analyze_hook(lambda s: {"ok": True, "bond_dim": 2,
                                   "n_components": 1, "note": "fake"})
    try:
        assert M.default_method("s", "2d")[0] == "pbesol"
    finally:
        M._set_analyze_hook(None)


def test_default_method_missing_struct_dim_conservative():
    M._set_analyze_hook(lambda s: {"ok": True, "bond_dim": 3,
                                   "n_components": 1, "note": "fake"})
    try:
        func, reason = M.default_method("s", None)
        assert func == "pbe-d3"
        assert "struct_dim 缺失" in reason
    finally:
        M._set_analyze_hook(None)


def test_default_method_bond_gt_struct_dim_conservative():
    # 键网络维度高于结构维度 = 与 struct_dim 冲突，不硬套等维结论 -> 保守 D3
    M._set_analyze_hook(lambda s: {"ok": True, "bond_dim": 3,
                                   "n_components": 1, "note": "fake"})
    try:
        func, reason = M.default_method("s", "2d")
        assert func == "pbe-d3"
        assert "高于结构维度" in reason
    finally:
        M._set_analyze_hook(None)


def test_default_method_accepts_dims_as_int_or_str():
    M._set_analyze_hook(lambda s: {"ok": True, "bond_dim": 3,
                                   "n_components": 1, "note": "fake"})
    try:
        assert M.default_method("s", 3)[0] == "pbesol"
        assert M.default_method("s", "3D")[0] == "pbesol"
    finally:
        M._set_analyze_hook(None)


def test_analyze_bond_network_without_pymatgen_ok_false():
    # 本机大概率无 pymatgen：返回 ok=False 且 note 说明，而不是抛异常
    res = M.analyze_bond_network("dummy")
    assert res["ok"] is False
    assert res["bond_dim"] is None
    assert res["n_components"] is None
    assert res.get("note")
    assert M.analyze_bond_network(None)["ok"] is False


# ----------------------------------------------------------------------
# 方法卡：make / write / 指纹
# ----------------------------------------------------------------------
def test_make_method_card_infers_func_and_captures_tags():
    d = _mk_incar_dir("GGA = PE\nIVDW = 12\nENCUT = 500\nPREC = Accurate\n"
                      "LASPH = .TRUE.\n", potcar_bytes=b"pot-1")
    try:
        card = M.make_method_card(d, struct_dim="3d", bond_dim=3,
                                  step="step1")
        assert card["func"] == "pbe-d3"          # 由 GGA/IVDW 反推
        assert card["tags"]["GGA"] == "PE"
        assert card["tags"]["IVDW"] == 12
        assert card["tags"]["ENCUT"] == 500
        assert card["tags"]["PREC"] == "Accurate"
        assert card["tags"]["LASPH"] is True
        assert card["struct_dim"] == 3
        assert card["bond_dim"] == 3
        assert card["metadata"]["step"] == "step1"
        assert card["potcar_sha1"] == M.potcar_sha1(os.path.join(d, "POTCAR"))
        assert len(card["fingerprint"]) == 64
        assert card["fingerprint"] == M.card_fingerprint(card)
    finally:
        _rm(d)


def test_make_method_card_func_incar_conflict_rejected():
    d = _mk_incar_dir("GGA = PS\nENCUT = 500\n")   # pbesol 的 GGA
    try:
        try:
            M.make_method_card(d, func="pbe-d3")     # 与 INCAR 冲突
            raise AssertionError("冲突未报错")
        except M.MethodCardError:
            pass
        # 显式给对就 OK
        ok = M.make_method_card(d, func="pbesol")
        assert ok["func"] == "pbesol"
    finally:
        _rm(d)


def test_make_method_card_no_incar_no_func_errors():
    d = tempfile.mkdtemp()
    try:
        try:
            M.make_method_card(d)                    # 无 INCAR 也推不出 func
            raise AssertionError("应报错")
        except M.MethodCardError:
            pass
    finally:
        _rm(d)


def test_write_and_reload_card_roundtrip():
    d = _mk_incar_dir("GGA = PE\nIVDW = 12\nENCUT = 500\n", potcar_bytes=b"pot")
    try:
        card = M.make_method_card(d, struct_dim="3d")
        path = M.write_method_card(card)
        assert os.path.basename(path) == M.CARD_NAME
        assert os.path.exists(path)
        loaded = M._coerce_card(path)
        assert loaded["func"] == card["func"]
        assert loaded["fingerprint"] == card["fingerprint"]
        # 目录形式也能读
        again = M._coerce_card(d)
        assert again["fingerprint"] == card["fingerprint"]
    finally:
        _rm(d)


def test_card_fingerprint_tracks_identity_only():
    d = _mk_incar_dir("GGA = PE\nIVDW = 12\nENCUT = 500\n")
    try:
        c1 = M.make_method_card(d, reason="r1", job="a")
        c2 = M.make_method_card(d, reason="换理由", job="b")   # reason/metadata 不算身份
        assert c1["fingerprint"] == c2["fingerprint"]
        c3 = dict(c2)
        c3["tags"] = dict(c2["tags"], ENCUT=520)              # 身份字段改动
        assert M.card_fingerprint(c3) != c2["fingerprint"]
    finally:
        _rm(d)


# ----------------------------------------------------------------------
# assert_method_consistent
# ----------------------------------------------------------------------
def _two_dirs(up_incar, cur_incar, up_pot=None, cur_pot=None):
    up = _mk_incar_dir(up_incar, potcar_bytes=up_pot)
    cur = _mk_incar_dir(cur_incar, potcar_bytes=cur_pot)
    return up, cur


def test_consistent_returns_empty():
    up, cur = _two_dirs("GGA = PE\nIVDW = 12\nENCUT = 500\n", "GGA = PE\nIVDW = 12\nENCUT = 500\n")
    try:
        up_card = M.write_method_card(M.make_method_card(up, struct_dim="3d"))
        cur_card = M.write_method_card(M.make_method_card(cur, struct_dim="3d"))
        assert M.assert_method_consistent(cur_card, up_card, "step2") == []
    finally:
        _rm(up); _rm(cur)


def test_inconsistent_raises_by_default():
    up, cur = _two_dirs("GGA = PE\nIVDW = 12\nENCUT = 500\n", "GGA = PS\nENCUT = 500\n")
    try:
        up_card = M.write_method_card(M.make_method_card(up, struct_dim="3d"))
        cur_card = M.write_method_card(M.make_method_card(cur, struct_dim="3d"))
        try:
            M.assert_method_consistent(cur_card, up_card, "step2_static")
            raise AssertionError("不一致却未报错")
        except M.MethodInconsistencyError as e:
            assert "step2_static" in str(e)
    finally:
        _rm(up); _rm(cur)


def test_waiver_needs_nonempty_reason_and_records():
    up, cur = _two_dirs("GGA = PE\nIVDW = 12\nENCUT = 500\n", "GGA = PS\nENCUT = 500\n")
    try:
        up_card = M.write_method_card(M.make_method_card(up, struct_dim="3d"))
        cur_card = M.write_method_card(M.make_method_card(cur, struct_dim="3d"))
        # 空理由不豁免
        for bad in (None, "", "   "):
            try:
                M.assert_method_consistent(cur_card, up_card, "s2", allow=bad)
                raise AssertionError("空理由竟然豁免了: %r" % (bad,))
            except M.MethodInconsistencyError:
                pass
        # 非空理由（str 全局豁免）=> 返回 waivers 并落盘记录
        waivers = M.assert_method_consistent(
            cur_card, up_card, "step2", allow="人工复核：本步改 PBEsol")
        assert waivers
        fields = {w["field"] for w in waivers}
        assert {"func", "tags"} <= fields
        assert all(w["reason"] == "人工复核：本步改 PBEsol" for w in waivers)
        log = os.path.join(cur, M.WAIVER_LOG_NAME)
        assert os.path.exists(log)
        with open(log, encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        assert any(x["step"] == "step2" for x in lines)
    finally:
        _rm(up); _rm(cur)


def test_waiver_dict_per_field():
    up, cur = _two_dirs("GGA = PE\nIVDW = 12\nENCUT = 500\n", "GGA = PE\nIVDW = 12\nENCUT = 520\n")
    try:
        up_card = M.write_method_card(M.make_method_card(up, struct_dim="3d"))
        cur_card = M.write_method_card(M.make_method_card(cur, struct_dim="3d"))
        # dict 只豁免列出的字段，未列字段（tags 变了）仍报错
        try:
            M.assert_method_consistent(cur_card, up_card, "s2",
                                       allow={"func": "换法但复核过"})
            raise AssertionError("未豁免字段却没报错")
        except M.MethodInconsistencyError:
            pass
        # 全字段豁免
        waivers = M.assert_method_consistent(
            cur_card, up_card, "s2",
            allow={"func": "x", "tags": "ENCUT 升到 520 已复核"})
        assert {w["field"] for w in waivers} == {"tags"}
    finally:
        _rm(up); _rm(cur)


def test_tampered_card_detected():
    up, cur = _two_dirs("GGA = PE\nIVDW = 12\nENCUT = 500\n", "GGA = PE\nIVDW = 12\nENCUT = 500\n")
    try:
        up_card = M.write_method_card(M.make_method_card(up, struct_dim="3d"))
        cur_card = M.write_method_card(M.make_method_card(cur, struct_dim="3d"))
        # 篡改源卡：改 tags 但不重算指纹
        tampered = json.loads(open(up_card, encoding="utf-8").read())
        tampered["tags"]["ENCUT"] = 999
        with open(up_card, "w", encoding="utf-8") as fh:
            json.dump(tampered, fh, ensure_ascii=False, indent=2)
        try:
            M.assert_method_consistent(cur_card, up_card, "step2",
                                       allow="什么理由都不行")
            raise AssertionError("篡改卡未被识破")
        except M.MethodCardTamperError:
            pass
    finally:
        _rm(up); _rm(cur)


def test_assert_accepts_dicts_without_directory():
    a = {"func": "pbesol", "tags": {"GGA": "PS"}, "potcar_sha1": None,
         "struct_dim": 3, "bond_dim": 3, "n_components": 1}
    b = dict(a)
    a["fingerprint"] = M.card_fingerprint(a)
    b["fingerprint"] = M.card_fingerprint(b)
    assert M.assert_method_consistent(a, b, "step") == []


# ----------------------------------------------------------------------
# validate_physical_constraints
# ----------------------------------------------------------------------
def test_constraint_ps_ivdw_without_d3_params():
    # PS + 非零 IVDW 却没给 VDW_S8/A1/A2 -> 拒绝
    bad = M.validate_physical_constraints(
        {"GGA": "PS", "IVDW": 12, "ISIF": 2}, struct_dim="3d")
    assert bad and "VDW_S8" in bad[0]
    # 给全三个参数 -> 通过
    ok = M.validate_physical_constraints(
        {"GGA": "PS", "IVDW": 12,
         "VDW_S8": -1.7, "VDW_A1": 0.4, "VDW_A2": 0.8, "ISIF": 2},
        struct_dim="3d")
    assert ok == []
    # PE + IVDW 12（pbe-d3 的正规组合）不需要那三个：规则只拦 PS
    ok2 = M.validate_physical_constraints(
        {"GGA": "PE", "IVDW": 12, "ISIF": 2}, struct_dim="3d")
    assert ok2 == []


def test_constraint_lowdim_isif3_needs_constraint():
    # 2D + ISIF=3 未声明真空约束 -> 拒绝
    bad = M.validate_physical_constraints({"GGA": "PS", "ISIF": 3},
                                          struct_dim="2d")
    assert bad and "ISIF=3" in bad[0]
    # vacuum_constrained=True 放行
    assert M.validate_physical_constraints({"GGA": "PS", "ISIF": 3},
                                           struct_dim="2d",
                                           vacuum_constrained=True) == []
    # 3D ISIF=3 没问题；0D/1D ISIF=2 没问题；无 struct_dim 时只跑 PS/IVDW 规则
    assert M.validate_physical_constraints({"GGA": "PS", "ISIF": 3},
                                           struct_dim="3d") == []
    assert M.validate_physical_constraints({"GGA": "PS", "ISIF": 2},
                                           struct_dim="0d") == []
    assert M.validate_physical_constraints({"GGA": "PS", "ISIF": 2},
                                           struct_dim="1d") == []
    assert M.validate_physical_constraints({"GGA": "PS", "ISIF": 3}) == []
    # 0D / 1D + ISIF=3 同样必须约束真空（规则写的是 0/1/2D）
    assert M.validate_physical_constraints({"GGA": "PS", "ISIF": 3},
                                           struct_dim="0d")
    assert M.validate_physical_constraints({"GGA": "PS", "ISIF": 3},
                                           struct_dim="1d")
    assert M.validate_physical_constraints({"GGA": "PS", "ISIF": 3},
                                           struct_dim="0d",
                                           vacuum_constrained=True) == []


def test_constraint_accepts_incar_dict_or_path():
    d = _mk_incar_dir("GGA = PS\nIVDW = 12\nISIF = 3\n")
    try:
        assert M.validate_physical_constraints(d, struct_dim="2d")
        assert M.validate_physical_constraints(
            os.path.join(d, "INCAR"), struct_dim="2d")
    finally:
        _rm(d)


# ----------------------------------------------------------------------
# 独立运行器（无 pytest 时也能跑）
# ----------------------------------------------------------------------
def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
            passed += 1
        except Exception as e:
            import traceback
            print("FAIL  " + name + "  ->  " + repr(e))
            traceback.print_exc()
            failed += 1
    print("\n%d passed, %d failed, %d total" % (passed, failed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
