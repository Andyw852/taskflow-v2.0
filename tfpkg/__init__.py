# -*- coding: utf-8 -*-
"""
tfpkg —— taskflow v2 包（由原单体脚本重构而来）。

架构：单一命名空间装配（single-namespace assembly）。
  原 versions/v1.0/tf 是一个 7463 行、184 个顶层定义的单体脚本，所有函数/
  常量共享一个模块级命名空间，互相按名字直接引用（含多处跨层循环依赖）。
  为了在「不改动任何函数体、行为完全等价」的前提下把它拆成可导航的文件，
  本包把原文件按职责切成 _slice/ 下的若干分片，并在本文件里按顺序在
  *同一个*命名空间中 exec 执行——效果等同原单文件。

导航（给 AI / 维护者）：
  - 改某个功能 → 按 _slice/ 文件名找对应分片（完整地图见仓库根 CONTEXT.md）。
  - 分片之间的函数引用按名字解析（和原单文件一致），不要给分片间加 import。
  - 装配顺序即依赖顺序：00_state 先注入标准库 import 与全部常量。
  - 若要真正把某分片升级为「深模块」（显式 import + 小接口），先读
    CONTEXT.md 的「循环依赖」一节，避免引入 import 环。
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SLICE = os.path.join(_HERE, "_slice")

# 关键：把分片代码里的 __file__ 指向 _slice 下的文件。它与原 versions/v1.0/tf
# 处于相同的相对深度（包根下第 3 层），保证代码里 dirname(__file__)/../.. 之类
# 的路径计算仍解析到包根（setting/ skill/）。
_NS = globals()
# 深模块化：已移除 __file__ 深度 hack（分片改用 _PKG_ROOT/_PKG_DIR 常量），
# __file__ 保持真实值 tfpkg/__init__.py。

# 深模块化：显式路径常量，替代分片里的 dirname(__file__)/../.. 计算。
# _PKG_ROOT = 包根（setting/ skill/ 所在），_PKG_DIR = tfpkg 包目录，
# _SLICE_DIR = _slice 目录。
_NS["_PKG_ROOT"] = os.path.normpath(os.path.dirname(_HERE))
_NS["_PKG_DIR"] = os.path.normpath(_HERE)
_NS["_SLICE_DIR"] = os.path.normpath(_SLICE)

# 深模块：真实模块（已抽离单命名空间的叶子）导入后，把接口注入共享命名空间。
# 这样剩余的 slice 仍按名字调用 _mini_yaml，行为不变。
import tfpkg.yamlmini as _yamlmini_mod
for _nm in ("_yaml_strip_comment", "_yaml_split_top", "_yaml_scalar",
            "_flow_depth", "_mini_yaml", "parse"):
    _NS[_nm] = getattr(_yamlmini_mod, _nm)
del _yamlmini_mod

# COLLECTOR（远端采集脚本）已独立成 tfpkg/_collector_remote.py —— 一个真实、
# 可 lint / 可单测的 .py 文件。这里读回成字符串注入命名空间，运行时字节与
# 原单体脚本里的 r'''...''' 字面量完全一致。
with open(os.path.join(_HERE, "_collector_remote.py"), encoding="utf-8") as _fh:
    _NS["COLLECTOR"] = _fh.read()

# v2.0：记录真实入口路径（bin/tf），供 --version / 裸 tf 的「程序:」行显示。
# __file__ 被指向 _slice/00_state.py 仅用于保持 dirname(__file__)/../.. 的
# 深度不变量，不应出现在面向用户的「程序:」里。
_prog = os.path.join(os.path.dirname(_HERE), "bin", "tf")
_NS["_PROG_PATH"] = (os.path.realpath(_prog)
                     if os.path.isfile(_prog) else _NS["__file__"])


def _load_slices():
    names = sorted(f for f in os.listdir(_SLICE) if f.endswith(".py"))
    for name in names:
        path = os.path.join(_SLICE, name)
        with open(path, encoding="utf-8") as fh:
            code = compile(fh.read(), path, "exec")
        exec(code, _NS)


_load_slices()

