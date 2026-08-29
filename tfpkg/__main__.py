# -*- coding: utf-8 -*-
"""python -m tfpkg 入口。"""
import os
import sys

from tfpkg import main

if __name__ == "__main__":
    try:
        main()
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)

