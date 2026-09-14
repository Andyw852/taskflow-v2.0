# -*- coding: utf-8 -*-
"""skills/_common/_corrections —— 纠错 handler 库（v1.0 建议 1.1）。

tf 启动后按需加载本目录下的 *.py：每个文件导出一个 HANDLER 实例
（CorrectionHandler 的子类实例），tf 用它把「诊断文本」映射成「该动什么」。

本文件不会被当作 handler（文件名以 __ 开头 / 无 HANDLER），只是包标记。
"""
