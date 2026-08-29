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

# ===== _yaml_strip_comment (原 L849-L858) =====
def _yaml_strip_comment(line):
    sq = dq = False
    for i, ch in enumerate(line):
        if ch == "'" and not dq:
            sq = not sq
        elif ch == '"' and not sq:
            dq = not dq
        elif ch == "#" and not sq and not dq and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line

# ===== _yaml_split_top (原 L861-L880) =====
def _yaml_split_top(s):
    """按顶层逗号切分（忽略引号/括号内的逗号）。"""
    parts, depth, sq, dq, cur = [], 0, False, False, ""
    for ch in s:
        if ch == "'" and not dq:
            sq = not sq
        elif ch == '"' and not sq:
            dq = not dq
        elif ch in "[{" and not sq and not dq:
            depth += 1
        elif ch in "]}" and not sq and not dq:
            depth -= 1
        if ch == "," and depth == 0 and not sq and not dq:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts

# ===== _yaml_scalar (原 L883-L911) =====
def _yaml_scalar(v):
    v = v.strip()
    if v in ("", "null", "Null", "NULL", "~"):
        return None
    if v in ("true", "True", "TRUE"):
        return True
    if v in ("false", "False", "FALSE"):
        return False
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_yaml_scalar(x) for x in _yaml_split_top(inner)] if inner else []
    if v.startswith("{") and v.endswith("}"):
        inner = v[1:-1].strip()
        d = {}
        for part in _yaml_split_top(inner):
            k, _, vv = part.partition(":")
            d[k.strip().strip("\"'")] = _yaml_scalar(vv)
        return d
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v

# ===== _flow_depth (原 L914-L929) =====
def _flow_depth(s, depth=0):
    """统计一行结束时未闭合的 [ / { 层数；跳过引号内内容和行尾注释。"""
    q = None
    for ch in s:
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#" and depth == 0:
            break
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth = max(0, depth - 1)
    return depth

# ===== _mini_yaml (原 L932-L1055) =====
def _mini_yaml(text):
    raw_lines = text.splitlines()
    blocks = {}
    lines = []
    i = 0
    while i < len(raw_lines):          # v3.13：支持 key: | / key: > 多行原文块
        raw = raw_lines[i]             # （kpoints.yaml 等需要原样保留换行的内容）
        s = _yaml_strip_comment(raw).rstrip()
        n = i + 1
        i += 1
        if not s.strip():
            continue
        indent = len(s) - len(s.lstrip(" "))
        stripped = s.strip()
        d = _flow_depth(stripped)          # v1.2：跨行的 [] / {} 拼成一行
        while d > 0 and i < len(raw_lines):
            nxt = _yaml_strip_comment(raw_lines[i]).strip()
            i += 1
            if not nxt:
                continue
            stripped = stripped.rstrip() + " " + nxt
            d = _flow_depth(nxt, d)
        mb = re.match(r"^([^-][^:]*):\s*([|>])[+-]?\s*$", stripped)
        if mb:
            key, style = mb.group(1).strip(), mb.group(2)
            chomp = "+" if stripped.endswith("+") else ("-" if stripped.endswith("-") else "")
            body = []
            while i < len(raw_lines):
                l2 = raw_lines[i]
                if not l2.strip():
                    body.append("")
                    i += 1
                    continue
                ind2 = len(l2) - len(l2.lstrip(" "))
                if ind2 > indent:
                    body.append(l2)
                    i += 1
                else:
                    break
            while body and not body[-1].strip():
                body.pop()
            nonempty = [l for l in body if l.strip()]
            base = min((len(l) - len(l.lstrip(" ")) for l in nonempty),
                       default=indent + 1)
            body = [l[base:] if l.strip() else "" for l in body]
            val = " ".join(l.strip() for l in body) if style == ">" else "\n".join(body)
            if chomp != "-":
                val += "\n"
            token = "\x00BLOCK%d\x00" % len(blocks)
            blocks[token] = val
            stripped = "%s: \"%s\"" % (key, token)
        lines.append([indent, stripped, n])
    pos = [0]

    def parse_block(indent):
        c = lines[pos[0]][1]
        return parse_list(indent) if (c == "-" or c.startswith("- ")) else parse_dict(indent)

    def parse_dict(indent):
        d = {}
        while pos[0] < len(lines):
            ind, content, n = lines[pos[0]]
            if ind < indent or content == "-" or content.startswith("- "):
                break
            if ind > indent:
                raise ValueError("配置第 %d 行缩进错误。" % n)
            key, _, val = content.partition(":")
            key, val = key.strip(), val.strip()
            pos[0] += 1
            if val:
                d[key] = _yaml_scalar(val)
            elif pos[0] < len(lines) and lines[pos[0]][0] > ind:
                d[key] = parse_block(lines[pos[0]][0])
            else:
                d[key] = None
        return d

    def parse_list(indent):
        lst = []
        while pos[0] < len(lines):
            ind, content, n = lines[pos[0]]
            if ind != indent or not (content == "-" or content.startswith("- ")):
                break
            item = content[1:].strip()
            pos[0] += 1
            if not item:
                lst.append(parse_block(lines[pos[0]][0])
                           if pos[0] < len(lines) and lines[pos[0]][0] > ind else None)
            elif ":" in item and item[0] not in "\"'{":
                k, _, v = item.partition(":")
                d = {k.strip(): _yaml_scalar(v) if v.strip() else None}
                while pos[0] < len(lines) and lines[pos[0]][0] > ind:
                    ind2, c2, n2 = lines[pos[0]]
                    if c2 == "-" or c2.startswith("- "):
                        break
                    k2, _, v2 = c2.partition(":")
                    k2, v2 = k2.strip(), v2.strip()
                    pos[0] += 1
                    if v2:
                        d[k2] = _yaml_scalar(v2)
                    elif pos[0] < len(lines) and lines[pos[0]][0] > ind2:
                        d[k2] = parse_block(lines[pos[0]][0])
                    else:
                        d[k2] = None
                lst.append(d)
            else:
                lst.append(_yaml_scalar(item))
        return lst

    if not lines:
        return {}
    result = parse_block(lines[0][0])
    if pos[0] < len(lines):
        raise ValueError("配置第 %d 行附近无法解析。" % lines[pos[0]][2])

    def restore(o):  # 还原多行块占位符
        if isinstance(o, str) and o in blocks:
            return blocks[o]
        if isinstance(o, list):
            return [restore(x) for x in o]
        if isinstance(o, dict):
            return {k: restore(v) for k, v in o.items()}
        return o
    return restore(result)

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

