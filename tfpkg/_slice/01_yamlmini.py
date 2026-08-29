# -*- coding: utf-8 -*-
# 01_yamlmini —— 迷你 YAML 解析器 + load_config 配置加载
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L849  _yaml_strip_comment
#   L861  _yaml_split_top
#   L883  _yaml_scalar
#   L914  _flow_depth
#   L932  _mini_yaml
#   L1058  load_config

# 注：YAML 解析器已抽成真实深模块 tfpkg/yamlmini.py，
# 由 __init__.py 导入后把 _mini_yaml 注入共享命名空间；
# 本文件只保留 load_config（依赖 CONFIG_SEARCH 常量）。

# ===== load_config (原 L1058-L1080) =====
def load_config(path):
    if path is None:
        for c in CONFIG_SEARCH:
            if os.path.isfile(c):
                path = c
                break
    if path is None:
        return {}, None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        sys.exit("错误：配置文件 %s 读取失败（%s）。" % (path, e.strerror or e))
    if path.endswith(".json"):
        return (json.loads(text) or {}), path
    try:
        import yaml
        return (yaml.safe_load(text) or {}), path
    except ImportError:
        try:
            return (_mini_yaml(text) or {}), path
        except ValueError as e:
            sys.exit("错误：本机 python 没有 PyYAML，内置解析器又报错：%s" % e)

