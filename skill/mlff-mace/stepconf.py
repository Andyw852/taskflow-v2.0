#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stepconf.py —— 步骤配置文件 step.conf 的解析 / 合并 / 类型化取值
================================================================
一个步骤一个文件夹，文件夹里一个 step.conf，文件里分节。全文只有一种语法：
INCAR 风格的 `KEY = VALUE`，行尾 # 或 ! 之后是注释。没有 JSON，没有特殊符号。

    [params]        脚本行为参数（不写进 INCAR）
    [submit]        覆盖 submit.sh 的 Slurm 行；留空 = 沿用模板原值
    [incar]         覆盖继承来的 INCAR 标签
    [incar.delete]  删除继承来的标签（每行一个标签名，不写等号）
    [incar.final]   在脚本自动计算(NBANDS/KPAR/MAGMOM…)之后再覆盖，你说了算
    [<自定义>]      结构化数据，每行按空白切分，如 [kpath.extra] 的 "X 0.5 0.0 0.0"

合并：tf 在本地按 skill 默认 → project templates/ → templates/<步骤>/ 逐级叠加，
把结果连同来源注释写成一份 step.conf 推到超算。gen 脚本只读这一份，不做回落。
"""

import re
import sys
from pathlib import Path

CONF_NAME = "step.conf"
_COMMENT = re.compile(r"\s+[#!].*$")
_SECTION = re.compile(r"^\[([A-Za-z0-9_.\-]+)\]$")

# 驱动层保留键：写在 [params] 里、供 tf 决定步骤图（如 BANDGAP=pbe|hse 增删
# 整段 HSE），gen 脚本本身不消费。校验白名单时无条件放行，避免各 gen 脚本
# 都误报"不认识的键"。新增工作流级开关往这里加即可。
RESERVED_PARAMS = frozenset({"BANDGAP", "AMSET_ENV", "POTCAR_DIR", "REFERENCES_DIR"})  # 3090 集群级默认键（VASP 赝势/凸包参考），漏进 mlff-mace 的 step.conf 需放行


def _strip(line):
    s = line.rstrip()
    if s.lstrip().startswith(("#", "!")):
        return ""
    return _COMMENT.sub("", s).strip()


def parse(text, src="<text>"):
    """-> {节名: [(key, value, 原始行号), ...]}；[incar.delete] 这类无等号的行 value=None。"""
    out, sec = {}, "params"
    for i, line in enumerate(text.splitlines(), 1):
        s = _strip(line)
        if not s:
            continue
        m = _SECTION.match(s)
        if m:
            sec = m.group(1).lower()
            out.setdefault(sec, [])
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            out.setdefault(sec, []).append((k.strip(), v.strip(), i))
        else:
            out.setdefault(sec, []).append((s, None, i))
    return out


def read_submit(path, cwd="."):
    """只读 step.conf 的 [submit] 节，返回 {key(小写, 连字符转下划线): value}。

    供那些不加载完整 step.conf（没有 [params] spec、只关心提交参数）的
    gen 脚本复用——绕开 StepConf 对 [params] 未知键的严格校验，只取 [submit]。
    文件不存在时返回 {}。value 过滤掉 None / 空。
    """
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd) / p
    if not p.is_file():
        return {}
    merged = parse(p.read_text(encoding="utf-8-sig"), str(p))
    return {k.lower().replace("-", "_"): v
            for k, v, _ in merged.get("submit", []) if v not in (None, "")}


_SUBMIT_FLAGS = {
    "nodes": "nodes", "ntasks_per_node": "ntasks-per-node",
    "ntasks": "ntasks", "cpus_per_task": "cpus-per-task",
    "qos": "qos", "partition": "partition", "time": "time",
    "job_name": "job-name", "gres": "gres", "mem": "mem",
}


def apply_submit(submit_path, sub_dict):
    """把 sub_dict 覆盖到 submit.sh 的 #SBATCH 行；无该行则在首个 #SBATCH 后补一行。

    统一的核数/队列覆盖出口：gen 脚本写 submit.sh 后调
        stepconf.apply_submit(submit_path, stepconf.read_submit(CONF_NAME))
    sub_dict 的 key 用下划线小写（read_submit 已把连字符转下划线），
    值为 None/空 = 跳过。sub_dict 空则不动。
    """
    changed = []
    if not sub_dict:
        return changed
    p = Path(submit_path)
    if not p.is_file():
        return changed
    text = p.read_text(encoding="utf-8")
    for k, v in sub_dict.items():
        if v in (None, ""):
            continue
        fl = _SUBMIT_FLAGS.get(k, k.replace("_", "-"))
        pat = re.compile(r"^(#SBATCH\s+--%s=)\S+.*$" % re.escape(fl), re.MULTILINE)
        if pat.search(text):
            text = pat.sub(r"\g<1>%s" % v, text)
        else:
            lines = text.splitlines()
            last = max([i for i, ln in enumerate(lines)
                        if ln.startswith("#SBATCH")] or [0])
            if last:
                lines.insert(last + 1, "#SBATCH --%s=%s" % (fl, v))
                text = "\n".join(lines) + "\n"
        changed.append("--%s=%s" % (fl, v))
    p.write_text(text, encoding="utf-8", newline="\n")
    return changed


def merge(sources):
    """sources = [(标签, 文本), ...]，越靠后优先级越高。
    -> (merged, prov)：merged 同 parse 的结构；prov[(节, key)] = 标签。"""
    merged, prov = {}, {}
    for tag, text in sources:
        for sec, items in parse(text, tag).items():
            cur = merged.setdefault(sec, [])
            for k, v, _ in items:
                if v is None:                       # 无等号的行（如 [incar.delete]）：追加去重
                    if all(x[0] != k for x in cur):
                        cur.append((k, None, 0))
                        prov[(sec, k)] = tag
                    continue
                for j, (ek, _, _) in enumerate(cur):
                    if ek.upper() == k.upper():
                        cur[j] = (ek, v, 0)
                        break
                else:
                    cur.append((k, v, 0))
                prov[(sec, k)] = tag
    return merged, prov


def dumps(merged, prov=None, header_lines=()):
    """把合并结果写回 step.conf 文本，每行标注来源。"""
    out = list(header_lines)
    order = (["params", "submit", "incar", "incar.delete", "incar.final"]
             + sorted(s for s in merged
                      if s not in ("params", "submit", "incar",
                                   "incar.delete", "incar.final")))
    for sec in order:
        items = merged.get(sec)
        if not items:
            continue
        out.append("")
        out.append("[%s]" % sec)
        w = max((len(k) for k, _, _ in items), default=1)
        wv = max((len(v) for _, v, _ in items if v is not None), default=1)
        for k, v, _ in items:
            src = (prov or {}).get((sec, k))
            if v is None:
                out.append("%-*s%s" % (w + wv + 3, k,
                                       ("  # <- %s" % src) if src else ""))
            else:
                out.append("%-*s = %-*s%s" % (w, k, wv, v,
                                              ("  # <- %s" % src) if src else ""))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ 类型化取值
def _as_bool(s):
    low = str(s).strip().lower()
    if low in ("true", ".true.", "yes", "on", "1"):
        return True
    if low in ("false", ".false.", "no", "off", "0"):
        return False
    raise ValueError("不是布尔值: %r" % s)


def _as_elemmap(s):
    """'Mn:5.0 In:0.0' -> {'Mn': 5.0, 'In': 0.0}"""
    out = {}
    for tok in str(s).replace(",", " ").split():
        if ":" not in tok:
            raise ValueError("元素表要写成 元素:数值，收到 %r" % tok)
        el, val = tok.split(":", 1)
        out[el.strip()] = float(val)
    return out


_CAST = {
    "str": lambda s: str(s),
    "int": lambda s: int(str(s), 0),
    "float": float,
    "bool": _as_bool,
    "elemmap": _as_elemmap,
    "words": lambda s: str(s).split(),
}


class StepConf(object):
    def __init__(self, merged, spec, path=None):
        self._m, self._spec, self.path = merged, spec, path
        self.params = {}
        raw = {k.upper(): v for k, v, _ in merged.get("params", [])}
        unknown = sorted(set(raw) - {k.upper() for k in spec} - RESERVED_PARAMS)
        if unknown:
            raise SystemExit("[ERROR] %s 的 [params] 里有本脚本不认识的键：%s\n"
                             "        可用键：%s"
                             % (path or CONF_NAME, ", ".join(unknown),
                                ", ".join(sorted(spec))))
        for key, (default, typ) in spec.items():
            s = raw.get(key.upper())
            if s is None or s == "":
                self.params[key] = None if (s == "" and default is not None
                                            and typ != "str") else default
                if s == "":
                    self.params[key] = None
                continue
            try:
                self.params[key] = _CAST[typ](s)
            except (ValueError, KeyError) as e:
                raise SystemExit("[ERROR] %s 的 %s=%r 解析失败（应为 %s）：%s"
                                 % (path or CONF_NAME, key, s, typ, e))

    def __getitem__(self, k):
        return self.params[k]

    def section(self, name):
        """任意节的原始行：[(key, value|None), ...]"""
        return [(k, v) for k, v, _ in self._m.get(name.lower(), [])]

    @property
    def incar(self):
        return {k.upper(): v for k, v, _ in self._m.get("incar", []) if v is not None}

    @property
    def incar_final(self):
        return {k.upper(): v for k, v, _ in self._m.get("incar.final", []) if v is not None}

    @property
    def incar_delete(self):
        return {k.upper() for k, _, _ in self._m.get("incar.delete", [])}

    @property
    def submit(self):
        return {k.lower(): v for k, v, _ in self._m.get("submit", [])
                if v not in (None, "")}

    def apply_incar(self, inherited, computed=None):
        """继承 → [incar] → 脚本计算 → [incar.final] → [incar.delete]"""
        out = dict(inherited)
        out.update(self.incar)
        out.update(computed or {})
        out.update(self.incar_final)
        for k in self.incar_delete:
            out.pop(k, None)
        return out


def load(spec, step_name=None, cwd="."):
    """gen 脚本入口：读材料目录里 tf 推来的那一份 step.conf。"""
    p = Path(cwd) / CONF_NAME
    if not p.is_file():
        raise SystemExit("[ERROR] 缺少 %s —— 该步骤的 gen_need 里漏了它？" % CONF_NAME)
    merged = parse(p.read_text(encoding="utf-8-sig"), str(p))
    got = {k.upper(): v for k, v, _ in merged.get("params", [])}.get("STEP")
    if step_name and got and got != step_name:
        raise SystemExit("[ERROR] %s 属于步骤 %r，本脚本是 %r —— gen_need 串了。"
                         % (p, got, step_name))
    spec = dict(spec)
    spec.setdefault("STEP", (step_name, "str"))
    return StepConf(merged, spec, str(p))


def _spans(lines):
    """-> [(节名, 起始行, 结束行)]；第一个 [xxx] 之前的内容算 params。"""
    out, cur, start = [], "params", 0
    for i, line in enumerate(lines):
        s = _strip(line)
        if s and _SECTION.match(s):
            out.append((cur, start, i))
            cur, start = _SECTION.match(s).group(1).lower(), i + 1
    out.append((cur, start, len(lines)))
    return out


def set_value(path, section, key, value):
    """就地改一个键：文件/节/键不存在则新建，注释与其它行原样保留。
    value=None 表示删除该键。返回 (旧值, 新值)。"""
    p = Path(path)
    lines = (p.read_text(encoding="utf-8-sig").splitlines()
             if p.is_file() else ["[params]"])
    section, ku = section.lower(), key.upper()
    hit, tail = None, None
    for name, a, b in _spans(lines):
        if name != section:
            continue
        for i in range(a, b):
            s = _strip(lines[i])
            if s and "=" in s and s.split("=", 1)[0].strip().upper() == ku:
                hit = i
        tail = b
        while tail > a and not lines[tail - 1].strip():
            tail -= 1
    old = lines[hit].split("=", 1)[1].strip() if hit is not None else None
    if value is None:
        if hit is not None:
            lines.pop(hit)
    elif hit is not None:
        keep = _COMMENT.search(lines[hit])
        lines[hit] = "%s = %s%s" % (lines[hit].split("=", 1)[0].rstrip(),
                                    value, keep.group(0) if keep else "")
    elif tail is not None:
        lines.insert(tail, "%s = %s" % (key, value))
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[%s]" % section, "%s = %s" % (key, value)]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return old, value
