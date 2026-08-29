# -*- coding: utf-8 -*-
"""tfpkg —— taskflow v2 包（深模块化重构完成）。

架构：真深模块（real deep modules，小接口 + 深实现）。
  原 versions/v1.0/tf 是 7463 行单体脚本。v2.0 先按职责切成 _slice/ 分片做
  单一命名空间装配，现已全部抽成真深模块（显式 import + 小接口）：

  - bootstrap.py  配置/发现流（原 00_state+01_yamlmini+02_skills+03_projects+04_discover）
  - collect.py    远端采集（原 05_collect）
  - data.py       数据簇（collect_data + 过滤 + 缓存 + 快照）
  - workflow.py   工作流执行引擎（原 06_state+09_submit+13_advance+11_actions）
  - report.py     渲染/汇报流（原 07_render+08_assets+10_summary）
  - ops.py        运维流（原 12_hang+14_init+15_hpc+16_watch）
  - cli.py        命令入口（原 17_cli）
  - yamlmini.py   YAML 解析器

  跨模块引用在函数内用 from tfpkg import ... 延迟解析（调用时才解析，避开
  模块级 import 环）；本文件把各模块的名字全部注入包命名空间，保证
  from tfpkg import X 在调用时都能命中。两个历史循环依赖环均已消除。
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_NS = globals()

# 路径常量：_PKG_ROOT = 包根（setting/ skill/ 所在），_PKG_DIR = tfpkg 目录，
# _SLICE_DIR = _slice 目录（已废弃，仅保留兼容）。
_NS["_PKG_ROOT"] = os.path.normpath(os.path.dirname(_HERE))
_NS["_PKG_DIR"] = os.path.normpath(_HERE)
_NS["_SLICE_DIR"] = os.path.normpath(os.path.join(_HERE, "_slice"))

# 导入全部真深模块。bootstrap 必须最先（report 模块级 from tfpkg.bootstrap
# import REASON_MAX 依赖它；其余模块无模块级跨模块依赖，顺序无关）。
from . import (  # noqa: E402
    bootstrap, collect, data, workflow, report, ops, cli, yamlmini,
)
_MODULES = (bootstrap, collect, data, workflow, report, ops, cli, yamlmini)

# 把各模块的名字注入包命名空间（排除 dunder 与标准库名）。
_STDLIB_NAMES = {
    "os", "sys", "re", "json", "time", "shlex", "hashlib", "base64",
    "collections", "functools", "itertools", "subprocess", "tempfile",
    "threading", "socket", "argparse", "glob", "math", "random", "shutil",
    "Counter", "defaultdict", "ThreadPoolExecutor", "datetime", "copy",
    "pathlib", "getpass", "textwrap", "urllib", "io", "string", "signal",
    "ast", "inspect", "warnings", "csv",
}
for _mod in _MODULES:
    for _nm, _obj in vars(_mod).items():
        if _nm.startswith("__") or _nm in _STDLIB_NAMES:
            continue
        _NS[_nm] = _obj
del _mod, _nm, _obj

# COLLECTOR（远端采集脚本）已独立成 tfpkg/_collector_remote.py —— 真实 .py 文件。
# 这里读回成字符串注入命名空间，运行时字节与原单体脚本里的 r'''...''' 字面量一致。
with open(os.path.join(_HERE, "_collector_remote.py"), encoding="utf-8") as _fh:
    _NS["COLLECTOR"] = _fh.read()

# v2.0：记录真实入口路径（bin/tf），供 --version / 裸 tf 的「程序:」行显示。
_prog = os.path.join(os.path.dirname(_HERE), "bin", "tf")
_NS["_PROG_PATH"] = (os.path.realpath(_prog)
                     if os.path.isfile(_prog) else _NS["__file__"])
