#!/usr/bin/env python3
"""脱敏：按 conf 的「源词=目标词」映射替换（长词优先防子串误伤）。
用法: python3 scripts/sanitize.py setting/git-sanitize.conf"""
import subprocess, sys

conf = sys.argv[1]
repls = []
for line in open(conf, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    a, b = line.split("=", 1)
    repls.append((a, b))
repls.sort(key=lambda x: -len(x[0]))  # 长词优先

pat = "|".join(r[0] for r in repls)
out = subprocess.run(
    ["git", "grep", "-l", "-E", pat, "--", "."],
    capture_output=True, text=True,
)
files = [f for f in out.stdout.split() if f]
for f in files:
    try:
        t = open(f, encoding="utf-8").read()
    except (UnicodeDecodeError, IsADirectoryError):
        continue
    for a, b in repls:
        t = t.replace(a, b)
    open(f, "w", encoding="utf-8").write(t)
print("脱敏文件数:", len(files))
