# -*- coding: utf-8 -*-
# 15_hpc —— hpc / level / auto / adopt / migrate-subdir 命令
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L5908  _write_hpc_yaml
#   L5926  cmd_hpc
#   L6016  _list_pkg_clusters
#   L6049  _level_stepconf_path
#   L6060  _level_write
#   L6091  cmd_level
#   L6135  cmd_auto_project
#   L6173  _skill_local_mats
#   L6193  cmd_auto_skill
#   L6213  _proj_setting_path
#   L6223  _set_yaml_bool
#   L6237  cmd_auto
#   L6278  cmd_adopt
#   L6391  cmd_migrate_subdir

# ===== _write_hpc_yaml (原 L5908-L5923) =====
def _write_hpc_yaml(path, d, note):
    """hpc.yaml 写出（tf 不依赖 PyYAML，手写简单结构；dict 值只到一层）。"""
    keys = [k for k in ("name", "ssh_host", "template_map") if k in d]
    keys += [k for k in d if k not in keys]
    lines = ["# %s\n" % note]
    for k in keys:
        v = d[k]
        if isinstance(v, dict):
            lines.append("%s:\n" % k)
            lines += ["  %s: %s\n" % (k2, v2) for k2, v2 in v.items()]
        elif isinstance(v, list):
            lines.append("%s: [%s]\n" % (k, ", ".join(str(x) for x in v)))
        else:
            lines.append("%s: %s\n" % (k, v))
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

# ===== cmd_hpc (原 L5926-L6013) =====
def cmd_hpc(cfg, types, projs, cluster, tt, yes):
    """v1.7：把 -p 指定的项目（一个或多个）分配到指定超算；未指定的项目一律不动。
      tf -p X,Y hpc <集群名>             材料级：改写 project_setting/hpc.yaml
                                         （该材料全部技能生效）
      tf -tt elastic -p X,Y hpc <集群名> 技能级：写/改 材料/<技能>/hpc.yaml
                                         （v1.6 私有配置，优先级最高）
    集群主配置 = 包内 setting/<集群名>.yaml（照 jzzn.yaml 建）；其 template_map
    指向的模板文件须能被找到（skill/<技能>/、project_setting/ 或 <技能>/）。"""
    if not cluster:
        print("错误：缺集群名。用法：tf -p 项目[,项目...] [-tt 技能] hpc <集群名>")
        return 1
    if not projs:
        print("错误：hpc 必须用 -p 显式指定项目（逗号分隔多个）；未指定的不动。")
        return 1
    master_path = pkg_setting_path(cluster + ".yaml")
    if not master_path:
        print("错误：没有集群主配置 setting/%s.yaml。照 jzzn.yaml 建一份："
              "name/ssh_host/template_map（指向 submit_%s_vaspstd_*.tpl 等，"
              "模板文件放进 skill/<技能>/ 目录）。可用集群：%s"
              % (cluster, cluster, _list_pkg_clusters()))
        return 1
    master = _load_yaml_file(master_path)
    if not master.get("ssh_host"):
        print("警告：%s 没写 ssh_host——提交不知道该连哪台。" % master_path)
    todo, seen = [], set()
    for t in types:
        root = t.get("local_root")
        if not root:
            continue
        _r, mats = discover_local(root)
        for m in mats:
            # 同一材料可能被多个配置段重复发现（主流程靠 _dedup_segments
            # 去重，这里自查）：hpc.yaml 按 材料+技能 写，一份就够
            key = (t["key"], os.path.realpath(m["lpath"]))
            if key in seen:
                continue
            resolve_material_local(t, root, m)
            if m["name"] in projs or os.path.basename(m["name"]) in projs:
                seen.add(key)
                todo.append((t, root, m))
    if not todo:
        print("错误：没找到 -p 指定的项目（%s）。" % ", ".join(projs))
        return 1
    print("将把 %d 个项目的%s分配到集群 %s（ssh_host=%s）："
          % (len(todo), (" [%s] 技能" % tt) if tt else "（全部技能）",
             master.get("name") or cluster, master.get("ssh_host") or "未写"))
    for t, root, m in todo:
        print("  %-24s → %s" % (m["name"], ("材料/%s/hpc.yaml" % t["key"])
                                 if tt else "project_setting/hpc.yaml"))
    if not yes:
        ans = input("确认执行？ [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    fails = 0
    for t, root, m in todo:
        if tt:
            sub = m.get("_subdir")
            if not sub:
                print("%s: 失败——技能 %s 没开 skill_subdir，写不了技能私有 "
                      "hpc.yaml（去掉 -tt 改材料级，或先开 subdir）"
                      % (m["name"], t["key"]))
                fails += 1
                continue
            tdir = os.path.join(m["lpath"], sub)
            os.makedirs(tdir, exist_ok=True)
        else:
            tdir = (m.get("ps") or {}).get("dir")
            if not tdir:
                print("%s: 失败——缺 project_setting（先 tf init）" % m["name"])
                fails += 1
                continue
        target = os.path.join(tdir, "hpc.yaml")
        new = _load_yaml_file(target) or {}
        new.update(master)   # 主配置字段全量覆盖；旧文件里的额外字段保留
        _write_hpc_yaml(target, new, "超算配置（tf hpc %s 于 %s 生成/更新）"
                        % (cluster, time.strftime("%Y-%m-%d %H:%M:%S")))
        resolve_material_local(t, root, m)   # 重新解析（带上新写的 hpc.yaml）再查模板
        missing = [lg for lg in (master.get("template_map") or {})
                   if not find_asset(cfg, t, m, lg)]
        note = ("；★ 模板缺失：%s——把文件放进 skill/%s/ 或 %s"
                % (", ".join(missing), t["key"], tdir)) if missing else ""
        print("%s[%s]: hpc → %s（%s）%s"
              % (m["name"], t["key"], master.get("name") or cluster,
                 master.get("ssh_host") or "未写", note))
    print("完成。验证：tf -tt %s 状态表 hpc 列应显示 %s。"
          % (tt or "<技能>", master.get("name") or cluster))
    return 1 if fails else 0

# ===== _list_pkg_clusters (原 L6016-L6025) =====
def _list_pkg_clusters():
    out = []
    for d in (os.path.join(_PKG_ROOT, "setting"),
              os.path.join(_PKG_DIR, "setting"),
              os.path.expanduser("~/.config/taskflow/setting")):
        if os.path.isdir(d):
            out += [f[:-5] for f in os.listdir(d)
                    if f.endswith(".yaml") and f != "tf_default.yaml"]
    return ", ".join(sorted(set(out))) or "（无）"

# ===== _level_stepconf_path (原 L6049-L6057) =====
def _level_stepconf_path(lpath, tkey):
    """<材料>/<技能>/project_setting/templates/step.conf（项目共用层）。"""
    if not lpath:
        return None
    for base in (os.path.join(lpath, str(tkey), "project_setting"),
                 os.path.join(lpath, "project_setting")):
        if os.path.isdir(base):
            return os.path.join(base, "templates", "step.conf")
    return None

# ===== _level_write (原 L6060-L6088) =====
def _level_write(path, level):
    """把 [params].BANDGAP 改成 level，其余内容与注释原样保留。"""
    note = ("# 计算级别（tf level 维护）：pbe = 只算到 step3（PBE/PBEsol，"
            "跳过整段 HSE）；hse = 继续算到 step4（HSE06）")
    line = "BANDGAP = %s" % level
    if os.path.isfile(path):
        src = open(path, encoding="utf-8-sig").read()
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        src = _LEVEL_HEADER
    out, hit, in_params = [], False, False
    for ln in src.splitlines():
        st = ln.strip()
        if st.startswith("[") and st.endswith("]"):
            in_params = (st.lower() == "[params]")
        if in_params and not hit and re.match(r"^\s*BANDGAP\s*=", ln):
            out.append(note)
            out.append(line)
            hit = True
            continue
        out.append(ln)
    if not hit:
        if not any(x.strip().lower() == "[params]" for x in out):
            out.append("")
            out.append("[params]")
        idx = max(i for i, x in enumerate(out)
                  if x.strip().lower() == "[params]")
        out[idx + 1:idx + 1] = [note, line]
    open(path, "w", encoding="utf-8").write("\n".join(out).rstrip() + "\n")

# ===== cmd_level (原 L6091-L6132) =====
def cmd_level(cfg, types, tt, proj, arg):
    """tf [-tt 技能] [-p 材料] level [pbe|hse] —— 设/查计算级别。"""
    keys = skill_keys(cfg, tt)
    wants = ([x.strip() for x in str(proj).split(",") if x.strip()]
             if proj else None)          # 不给 -p = 该技能下全部材料
    level = None
    if arg is not None:
        level = _LEVEL_ALIAS.get(str(arg).strip().lower())
        if level is None:
            print("错误：level 只接受 pbe / hse（也认 step3 / step4；收到 %r）。"
                  % arg)
            return 1
    fails = 0
    for k in keys:
        names = wants if wants is not None else _skill_local_mats(cfg, types, k)
        if not names:
            print("技能 %s：没发现材料。" % k)
            fails += 1
            continue
        print("技能 %s%s" % (k, ("  ->  %s（%s）" % (level, _LEVEL_DESC[level]))
                            if level else "  当前级别："))
        for w in names:
            lp = resolve_mat_dir(cfg, types, k, w)
            scp = _level_stepconf_path(lp, k)
            if not scp:
                print("  %-28s 还没 init（先 tf -tt %s -p %s init）" % (w, k, w))
                fails += 1
                continue
            cur = _stepconf_param_from_file(scp, "BANDGAP")
            eff = _LEVEL_ALIAS.get(str(cur or "").lower())
            if level is None:
                print("  %-28s %-4s %s"
                      % (w, eff or "hse",
                         "（step.conf 未写，用 skill 出厂默认）" if eff is None
                         else "（%s）" % scp))
                continue
            _level_write(scp, level)
            print("  %-28s %s -> %s" % (w, eff or "(未写)", level))
    if level:
        print("下次 tf / tf start 装配步骤图时生效。已跑完的 step4 产物不会被"
              "删除，只是不再出现在状态表里。")
    return fails

# ===== cmd_auto_project (原 L6135-L6170) =====
def cmd_auto_project(cfg, types, proj, tt, arg):
    """v1.9.9：tf [-tt X] -p 材料 auto on|off —— 改该技能项目的
    project_setting/setting.yaml，不动全局 tf.yaml。"""
    keys = skill_keys(cfg, tt)
    wants = [x.strip() for x in str(proj).split(",") if x.strip()]
    if arg is None:
        for w in wants:
            for k in keys:
                lp = resolve_mat_dir(cfg, types, k, w)
                f = _proj_setting_path(lp, k) if lp else None
                cur = (_load_yaml_file(f).get("auto_advance")
                       if f and os.path.isfile(f) else None)
                print("  %-14s %-9s %s" % (w, k, "（无配置）" if not f or
                      not os.path.isfile(f) else
                      ("开" if cur is True else "关")))   # autonow：缺这行 = 关
        return 0
    a = str(arg).strip().lower()
    if a not in ("on", "off", "1", "0", "true", "false", "开", "关"):
        print("错误：auto 只接受 on/off（收到 %r）。" % arg)
        return 1
    on = a in ("on", "1", "true", "开")
    fails = 0
    for w in wants:
        for k in keys:
            lp = resolve_mat_dir(cfg, types, k, w)
            f = _proj_setting_path(lp, k) if lp else None
            if not f or not os.path.isfile(f):
                print("  %s[%s]：还没有 project_setting，先 tf -tt %s -p %s init"
                      % (w, k, k, w))
                fails += 1
                continue
            _set_yaml_bool(f, "auto_advance", on)
            print("  %s[%s]：auto_advance = %s" % (w, k, "true" if on else "false"))
    if not cfg.get("auto_advance"):
        print("注意：全局 auto_advance 还是关的，本开关要配合 tf auto on 才生效。")
    return fails

# ===== _skill_local_mats (原 L6173-L6190) =====
def _skill_local_mats(cfg, types, tt):
    """patch_auto：列出该技能下本地已发现的材料名（纯本地，不连超算）。"""
    names, seen = [], set()
    for t0 in (types or []):
        if tt and t0.get("key") != tt:
            continue
        lr = t0.get("local_root")
        if not lr:
            continue
        try:
            _r, mats = discover_local(lr)
        except Exception:   # noqa: BLE001
            continue
        for mm in mats:
            if mm["name"] not in seen:
                seen.add(mm["name"])
                names.append(mm["name"])
    return names

# ===== cmd_auto_skill (原 L6193-L6210) =====
def cmd_auto_skill(cfg, types, tt, arg):
    """patch_auto：tf -tt <技能> auto [on|off] —— 对该技能下全部材料批量
    开关项目级 auto_advance；on 时顺手把全局 tf.yaml 也打开。"""
    names = _skill_local_mats(cfg, types, tt)
    if not names:
        print("没有在技能 %s 下发现任何材料（检查 project_roots / local_root）。"
              % tt)
        return 1
    if arg is None:
        print("全局 auto_advance：%s"
              % ("开" if cfg.get("auto_advance") else "关"))
        return cmd_auto_project(cfg, types, ",".join(names), tt, None)
    if str(arg).strip().lower() in ("on", "1", "true", "开"):
        if not cfg.get("auto_advance"):
            cmd_auto(cfg, "on")          # 先开全局，避免下面误报"全局还是关的"
            cfg["auto_advance"] = True
    print("技能 %s：共 %d 个材料 → %s" % (tt, len(names), ", ".join(names)))
    return cmd_auto_project(cfg, types, ",".join(names), tt, arg)

# ===== _proj_setting_path (原 L6213-L6220) =====
def _proj_setting_path(lpath, tkey):
    """材料目录下该技能的 setting.yaml 路径（技能子目录优先，回落材料级）。"""
    if not lpath:
        return None
    a = os.path.join(lpath, str(tkey), "project_setting", "setting.yaml")
    if os.path.isfile(a):
        return a
    return os.path.join(lpath, "project_setting", "setting.yaml")

# ===== _set_yaml_bool (原 L6223-L6234) =====
def _set_yaml_bool(path, key, on):
    """就地改（或追加）一个顶层布尔行，其余内容原样保留。"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    for i, ln in enumerate(lines):
        if re.match(r"^%s\s*:" % re.escape(key), ln):
            lines[i] = "%s: %s\n" % (key, "true" if on else "false")
            break
    else:
        lines.append("%s: %s\n" % (key, "true" if on else "false"))
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

# ===== cmd_auto (原 L6237-L6275) =====
def cmd_auto(cfg, arg):
    """v1.5 tf auto [on|off]：一键开关自动提交（改写全局 tf.yaml 的
    auto_advance 行；没有该行则补在文件头）。无参数 = 显示当前状态。
    只影响 auto_advance；后台监控（auto_watch）不受影响。"""
    path = cfg.get("_config_path")
    if not path:
        print("错误：没有找到配置文件。")
        return 1
    if arg is None:
        print("auto_advance 当前：%s（%s）"
              % ("开" if cfg.get("auto_advance") else "关", path))
        print("切换：tf auto on / tf auto off")
        return 0
    a = str(arg).strip().lower()
    if a not in ("on", "off", "1", "0", "true", "false", "开", "关"):
        print("错误：auto 只接受 on/off（收到 '%s'）。" % arg)
        return 1
    on = a in ("on", "1", "true", "开")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for i, ln in enumerate(lines):
            if re.match(r"^auto_advance\s*:", ln):
                lines[i] = "auto_advance: %s\n" % ("true" if on else "false")
                break
        else:
            lines.insert(0, "auto_advance: %s\n" % ("true" if on else "false"))
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as e:
        print("错误：读写配置失败：%s" % e)
        return 1
    print("auto_advance 已%s（%s）。" % ("开启" if on else "关闭", path))
    if on:
        print("status/watch 会自动提交可开始的步骤；手动 start/retry/rerun 不受影响。")
    else:
        print("status/watch 只看不提交；手动 start/retry/rerun 不受影响。")
        print("后台监控仍在跑（只拉结果）；停监控用 tf watch --stop。")
    return 0

# ===== cmd_adopt (原 L6278-L6388) =====
def cmd_adopt(cfg, types, proj, yes, dry, tt):
    """v1.5：接管手工整理的技能子目录结构。适用场景：人手工把 POSCAR、
    project_setting、result、log 搬进了 材料/<技能>/。tf 的规矩是 POSCAR 和
    project_setting 必须在材料根（所有技能共用），<技能>/ 里只放该技能产物。
      第 1 步（本地修正）：POSCAR、project_setting 挪回材料根；
                          根上残留的 result/log 挪进 <技能>/；
      第 2 步（并入迁移）：重新载入配置+采集，逐材料 migrate-subdir——远端
                          step* 移进 <技能>/、项目配置开 skill_subdir；
                          有作业在跑的跳过，算完再跑一次 adopt 即可。
    用法：tf -tt band adopt [--dry-run] [-y] [-p MAT]"""
    if not tt:
        print("错误：adopt 需要 -tt 指定接管哪个技能（如 tf -tt band adopt）。")
        return 1
    raw_tt = ((cfg.get("task_types") or {}).get(tt) or {})
    sub = str(raw_tt.get("dir_name") or tt)
    roots = [os.path.expanduser(r) for r in (cfg.get("project_roots") or [])]
    if not roots:
        print("错误：全局 tf.yaml 没有 project_roots。")
        return 1
    # ---- 第 1 步：扫 <root>/**/<sub>，上一级即材料目录 ----
    targets, plans = [], {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for depth in ("*", "*/*", "*/*/*"):
            for sd in sorted(glob.glob(os.path.join(root, depth, sub))):
                if not os.path.isdir(sd):
                    continue
                D = os.path.dirname(sd)
                if proj and os.path.relpath(D, root) != proj \
                        and os.path.basename(D) != proj:
                    continue
                if (D, root) in targets:
                    continue
                targets.append((D, root))
                fixes = []
                if (not os.path.isfile(os.path.join(D, "POSCAR"))
                        and os.path.isfile(os.path.join(sd, "POSCAR"))):
                    fixes.append((os.path.join(sd, "POSCAR"),
                                  os.path.join(D, "POSCAR"),
                                  "POSCAR 挪回材料根（多技能共用，必须在根）"))
                psd = os.path.join(sd, "project_setting")
                if (os.path.isdir(psd)
                        and not os.path.isdir(os.path.join(D, "project_setting"))):
                    fixes.append((psd, os.path.join(D, "project_setting"),
                                  "project_setting 挪回材料根（band/elastic 段都在里面）"))
                elif os.path.isdir(psd):
                    print("警告：%s 材料根和 %s/ 下各有一份 project_setting，"
                          "adopt 不动——请人工合并后删掉 %s 下那份（保留材料根的），"
                          "否则 %s/ 会被识别成名叫 %s 的新材料。"
                          % (D, sub, sd, sub, sub))
                for d in ("result", "log"):
                    rd = os.path.join(D, d)
                    if os.path.isdir(rd):
                        fixes.append((rd, os.path.join(sd, d),
                                      "%s/ 挪进 %s/" % (d, sub)))
                if fixes:
                    plans[D] = fixes
    if not targets:
        print("没有找到含 %s/ 子目录的材料目录（%s）。" % (sub, ", ".join(roots)))
        return 0
    if plans:
        print("第 1 步：本地布局修正（%d 个材料）：" % len(plans))
        for D, fixes in plans.items():
            for src, dst, why in fixes:
                print("  %-14s %s → %s" % (os.path.basename(D), src, why))
    else:
        print("第 1 步：本地布局无需修正。")
    if dry:
        print("（--dry-run，未执行）")
        return 0
    if plans and not yes:
        ans = input("执行以上移动？ [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    for D, fixes in plans.items():
        for src, dst, _why in fixes:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        print("%s: 布局已修正" % os.path.basename(D))
    # ---- 第 2 步：重新载入配置（第 1 步可能挪回了 project_setting）→ 采集 → 迁移 ----
    print("第 2 步：远端 step* 进 %s/ + 项目配置开 skill_subdir：" % sub)
    cfg2, _ = load_config(cfg.get("_config_path"))
    for k in ("host", "user"):
        if cfg.get(k) is not None:
            cfg2[k] = cfg[k]
    cfg2["_config_dir"] = cfg["_config_dir"]
    cfg2["_config_path"] = cfg["_config_path"]
    cfg2 = merge_project_configs(cfg2)
    types2 = get_types(cfg2, tt=tt)
    if not types2:
        print("错误：%s 还没有任何项目配置段——先 tf init，再重跑 adopt。" % tt)
        return 1
    data2 = collect_data(cfg2, types2)
    by_name = {m["name"]: m for t2 in data2["types"] for m in t2["materials"]}
    fails = 0
    for D, root in targets:
        rel = os.path.relpath(D, root)
        m = by_name.get(rel)
        if m is None:
            print("%s: 跳过——未被识别为材料（材料根缺 POSCAR？补好后 tf init）" % rel)
            continue
        if not (m.get("ps") or {}).get("dir"):
            print("%s: 跳过——缺 project_setting（先 tf -tt %s -p %s init，"
                  "再重跑 adopt）" % (rel, tt, rel))
            continue
        fails += cmd_migrate_subdir(cfg2, data2, rel, True, False)
    print("adopt 完成。用 tf -tt %s 核对：算好的应显示 done；被跳过的"
          "（在跑/缺配置）处理完再跑一次 tf -tt %s adopt -y。" % (tt, tt))
    return 1 if fails else 0

# ===== cmd_migrate_subdir (原 L6391-L6483) =====
def cmd_migrate_subdir(cfg, data, proj, yes, dry):
    """v1.2：把该技能已完成材料的数据迁进技能子目录（跟着项目走的目录结构）。
    远端 work/材料/step* → work/材料/<技能>/step*；本地 result、log → 材料/<技能>/；
    项目配置该技能段加 skill_subdir: true（状态随即按新路径采集，保持 done）。
    批量模式只迁全部完成的材料；有作业在跑的一律跳过；-p 指定放宽为无作业即可迁。"""
    t = data["types"][0]
    key, sub = t["key"], str(t.get("dir_name") or t["key"])
    mats = [m for m in t["materials"]
            if not proj or m["name"] == proj
            or os.path.basename(m["name"]) == proj]
    if proj and not mats:
        print("错误：%s 下没有材料 %s。" % (key, proj))
        return 1
    todo, skipped = [], []
    for m in mats:
        if m.get("_subdir"):
            skipped.append((m, "已是 %s/ 子目录结构" % sub))
            continue
        jobs = [s for s in m["steps"] if s.get("job")]
        if jobs:   # v1.4.1：jobs 装的是步骤不是作业——取 jobs[0]["job"]["id"]，
                   # 此前 jobs[0]["id"] 直接 KeyError（有在跑作业时崩溃）
            skipped.append((m, "有作业在跑（jobid=%s），算完再迁"
                            % jobs[0]["job"]["id"]))
            continue
        if proj is None and not _mat_all_done(m):
            skipped.append((m, "未全部完成（要迁单个：-p %s migrate-subdir）"
                            % m["name"]))
            continue
        todo.append(m)
    for m, why in skipped:
        print("%s: 跳过——%s" % (m["name"], why))
    if not todo:
        print("没有可迁移的材料。")
        return 0
    print("将把 %d 个材料的 %s 数据迁进 %s/ 子目录：" % (len(todo), key, sub))
    for m in todo:
        print("  %-14s 远端 %s/step* → %s/%s/step*；本地 result、log → %s/"
              % (m["name"], m["path"], m["path"], sub,
                 os.path.join(m["lpath"], sub)))
    if dry:
        print("（--dry-run，未执行）")
        return 0
    if not yes:
        ans = input("确认迁移？ [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return 1
    fails = 0
    for m in todo:
        host = m.get("host_eff") or "__default__"
        tag = "%s[%s]" % (m["name"], key)
        rc, out = run_remote(
            cfg, "cd %s && mkdir -p %s && for d in step*/; do "
                 "[ -d \"$d\" ] && mv \"$d\" %s/; done; true"
                 % (shlex.quote(m["path"]), shlex.quote(sub), shlex.quote(sub)),
            host=host)
        if rc != 0:
            print("%s: 远端迁移失败。%s" % (tag, out))
            fails += 1
            continue
        lp = m["lpath"]
        sdir = os.path.join(lp, sub)
        os.makedirs(sdir, exist_ok=True)
        moved = []
        for d in ("result", "log"):
            srcd = os.path.join(lp, d)
            if os.path.isdir(srcd):
                shutil.move(srcd, os.path.join(sdir, d))
                moved.append(d)
        m["log_dir"] = os.path.join(sdir, "log")   # log 已随迁，改指新位置
        # 项目配置该技能段开 skill_subdir——仅材料级配置才改；
        # 体系级共享配置（多材料共用）改了会误伤未迁的兄弟材料，提示手工处理
        ps = (m.get("ps") or {}).get("dir")
        own_ps = (ps and os.path.isdir(ps) and os.path.dirname(
            os.path.realpath(ps)) == os.path.realpath(lp))
        cfg_note = ""
        if own_ps:
            f0s = glob.glob(os.path.join(ps, "tf_*.yaml"))
            if f0s and _yaml_type_block_ensure(f0s[0], key,
                                               "    skill_subdir: true"):
                cfg_note = "，配置已开 skill_subdir"
            else:
                cfg_note = "，配置已有 skill_subdir"
        else:
            cfg_note = ("，注意：%s，请手工给 %s 段加 skill_subdir: true"
                        % ("项目配置为多材料共享" if ps else
                           "未找到材料级 project_setting", key))
        print("%s: 已迁移（远端 step* + 本地 %s%s）"
              % (tag, "/".join(moved) if moved else "无本地文件", cfg_note))
        log_action(m, "migrate-subdir → %s/（远端 step* + 本地 %s）"
                   % (sub, "/".join(moved)))
    print("迁移完成。tf 查看状态应仍为 done；挂 elastic：tf -tt elastic init")
    return 1 if fails else 0

