#!/usr/bin/env python3
"""
GTNH Daily Build Server Updater
================================
对比并更新来自新 daily build 压缩包的 mods 和 config（服务端）。
将此脚本放置于服务端根目录下运行。

用法:
  python update_daily_server.py              # 自动查找最新的服务端 build
  python update_daily_server.py --dry-run    # 仅预览，不做任何修改
  python update_daily_server.py --zip <path> # 指定压缩包路径
"""

import os
import sys
import re
import shutil
import zipfile
import io
import argparse
from datetime import datetime

# 下载目录（daily build 压缩包所在位置）
DOWNLOADS_DIR = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), "Downloads"
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODS_DIR = os.path.join(SCRIPT_DIR, "mods")
CONFIG_DIR = os.path.join(SCRIPT_DIR, "config")
BACKUP_DIR = os.path.join(SCRIPT_DIR, "back")
UPDATE_CFG_PATH = os.path.join(SCRIPT_DIR, "update_daily.cfg")


# ─────────────────────── 工具函数 ───────────────────────

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def load_exclude_add_list():
    """
    从 update_daily.cfg 加载新增排除列表。
    配置文件格式: [exclude_add] 段下每行一个 mod 名称（normalize 后匹配）。
    若文件不存在则自动创建默认配置。
    """
    default_content = """\
# update_daily.cfg — 更新工具配置文件
# 此文件控制更新脚本的行为。

[exclude_add]
# 以下 mod 在新增时将被排除（不会自动添加），但更新时不受影响。
# 每行一个 mod 的标准化名称（不含版本号，大小写不敏感）。
# 示例:
# SomeMod
# AnotherMod
"""
    if not os.path.exists(UPDATE_CFG_PATH):
        with open(UPDATE_CFG_PATH, "w", encoding="utf-8") as f:
            f.write(default_content)
        log(f"已生成默认配置文件: {UPDATE_CFG_PATH}")

    exclude = set()
    in_section = False
    with open(UPDATE_CFG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                in_section = (line.lower() == "[exclude_add]")
                continue
            if in_section and line and not line.startswith("#"):
                exclude.add(line.lower())
    return exclude


def find_latest_daily_zip(search_dirs):
    """
    在给定的目录列表中查找最新的 gtnh-*-server-*.zip（服务端包）。
    支持 daily / experimental 等构建类型，以及 java8 / new-java 变体。
    按目录优先级依次搜索，找到即返回。
    """
    pat = re.compile(
        r"gtnh-\w+-(\d{4}-\d{2}-\d{2})\+(\d+)-server[-\w]*\.zip$", re.I
    )
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        best = None
        for f in os.listdir(d):
            m = pat.match(f)
            if m:
                key = (m.group(1), int(m.group(2)))
                if best is None or key > best[0]:
                    best = (key, f)
        if best:
            return os.path.join(d, best[1])
    return None


def parse_build_info(zip_filename):
    """
    从压缩包文件名中提取构建类型和构建号。
    例如: gtnh-daily-2026-04-17+462-server-new-java.zip → ("daily", "462")
          gtnh-experimental-2026-04-17+105-server.zip → ("experimental", "105")
    """
    basename = os.path.basename(zip_filename)
    m = re.match(r"gtnh-(\w+)-\d{4}-\d{2}-\d{2}\+(\d+)-", basename, re.I)
    if m:
        return m.group(1), m.group(2)
    return None, None


def update_server_motd(build_type, build_number, dry_run=False):
    """
    更新 server.properties 中的 motd 行。
    将 motd=GT\:New Horizons xxx NNN 替换为新的构建类型和构建号。
    """
    props_path = os.path.join(SCRIPT_DIR, "server.properties")
    if not os.path.isfile(props_path):
        log("未找到 server.properties，跳过 motd 更新", "WARN")
        return

    with open(props_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    motd_pat = re.compile(r"^(motd=GT\\:New Horizons )\w+ \d+(.*)$")
    new_motd_value = f"{build_type} {build_number}"
    updated = False

    for i, line in enumerate(lines):
        m = motd_pat.match(line.rstrip("\n\r"))
        if m:
            old_line = line.rstrip("\n\r")
            lines[i] = f"{m.group(1)}{new_motd_value}{m.group(2)}\n"
            new_line = lines[i].rstrip("\n\r")
            if old_line != new_line:
                log(f"  motd: {old_line}")
                log(f"     → {new_line}")
                updated = True
            else:
                log("  motd 无变化")
                return
            break

    if not updated:
        log("未找到匹配的 motd 行（预期格式: motd=GT\\:New Horizons <type> <number>）", "WARN")
        return

    if not dry_run:
        with open(props_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        log("  server.properties 已更新")


def open_inner_zip(outer_path):
    """打开外层 zip，返回内层 zip 的 ZipFile 对象。"""
    outer = zipfile.ZipFile(outer_path, "r")
    inner_name = next(
        (n for n in outer.namelist()
         if n.upper().startswith("GTNH-") and n.lower().endswith(".zip")),
        None,
    )
    if not inner_name:
        raise FileNotFoundError("在外层 zip 中未找到内层 GTNH zip")
    log(f"内层 zip: {inner_name}")
    return zipfile.ZipFile(io.BytesIO(outer.read(inner_name)))


# ─────────────────────── Mod 名称/版本解析 ───────────────────────

def normalize_mod_name(filename):
    """
    提取 mod 的标准化基础名称（小写、不含版本号），用于匹配新旧 mod。
    例如:
      gregtech-5.09.52.396.jar         -> gregtech
      Draconic-Evolution-1.5.19-GTNH.jar -> draconic-evolution
      appliedenergistics2-rv3-beta-885-GTNH.jar -> appliedenergistics2-rv3
      BiblioCraft[v1.11.7][MC1.7.10].jar -> bibliocraft
    """
    name = filename[:-4] if filename.endswith(".jar") else filename
    # 移除方括号内容
    name = re.sub(r"\[.*?\]", "", name)
    # 移除开头的 +
    name = name.lstrip("+")

    parts = re.split(r"[-_]", name)
    base = []
    for p in parts:
        # 如果这部分看起来像版本号，就停止
        if re.match(r"^v?\d", p) and not re.match(r"^mc\d", p, re.I):
            break
        # ALPHA / beta / pre / rc 也是版本开始的标志
        if re.match(r"^(ALPHA|beta|pre|rc)\b", p, re.I):
            break
        base.append(p)

    if not base:
        base = [parts[0]]
    return "-".join(base).lower().strip("-").strip(".")


def extract_version_parts(filename):
    """
    从文件名中提取版本字符串并解析为可比较的元组。
    返回 (version_string, parsed_tuple)
    """
    name = filename[:-4] if filename.endswith(".jar") else filename
    name = re.sub(r"\[.*?\]", "", name)
    name = name.lstrip("+")

    # 找到版本开始的位置
    match = re.search(r"[-_]v?(\d[\d.]*\w*(?:[-_.+]\w+)*)", name)
    if not match:
        return ("0", ((0, 0),))

    ver_str = match.group(1)
    # 移除尾部的 -GTNH, -universal, -fix 等后缀（不含版本信息）
    ver_str = re.sub(r"-(GTNH|universal|fix|gtnh|forge|pre|unlimited|fairplay)$",
                     "", ver_str, flags=re.I)

    tokens = re.split(r"[.\-_+]", ver_str)
    parsed = []
    for t in tokens:
        try:
            parsed.append((0, int(t)))
        except ValueError:
            m = re.match(r"^(\d+)(.+)$", t)
            if m:
                parsed.append((0, int(m.group(1))))
                parsed.append((1, m.group(2).lower()))
            else:
                parsed.append((1, t.lower()))
    return (ver_str, tuple(parsed))


def is_newer_version(new_file, old_file):
    """判断 new_file 的版本是否高于 old_file。"""
    _, new_v = extract_version_parts(new_file)
    _, old_v = extract_version_parts(old_file)
    return new_v > old_v


# ─────────────────────── Mod 匹配 ───────────────────────

def match_mods(current_mods, new_mods):
    """
    匹配新旧 mod 列表。
    返回 [(action, cur_file, new_file), ...] 列表。
    action: 'keep'(完全相同), 'update'(版本不同), 'add'(新增), 'extra'(用户自行添加)
    """
    cur_set = set(current_mods)
    new_set = set(new_mods)

    # 精确匹配
    exact = cur_set & new_set
    results = [("keep", m, m) for m in exact]

    # 按标准化名称建立索引
    cur_by_name = {}
    for m in current_mods:
        if m not in exact:
            cur_by_name.setdefault(normalize_mod_name(m), []).append(m)

    new_by_name = {}
    for m in new_mods:
        if m not in exact:
            new_by_name.setdefault(normalize_mod_name(m), []).append(m)

    matched_cur = set()
    matched_new = set()

    for norm, new_files in new_by_name.items():
        if norm not in cur_by_name:
            continue
        for nf in new_files:
            if nf in matched_new:
                continue
            for cf in cur_by_name[norm]:
                if cf in matched_cur:
                    continue
                results.append(("update", cf, nf))
                matched_cur.add(cf)
                matched_new.add(nf)
                break

    # 仅在新包中的 mod → add
    for m in new_mods:
        if m not in exact and m not in matched_new:
            results.append(("add", None, m))

    # 仅在当前的 mod → extra（用户自行添加的）
    for m in current_mods:
        if m not in exact and m not in matched_cur:
            results.append(("extra", m, None))

    return results


# ─────────────────────── Mod 备份 ───────────────────────

def backup_mods(dry_run=False):
    """备份当前所有 mod jar 到 back/mods_时间戳/ 目录。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_dir = os.path.join(BACKUP_DIR, f"mods_{ts}")

    jars = [f for f in os.listdir(MODS_DIR) if f.endswith(".jar")]
    if dry_run:
        log(f"[DRY-RUN] 将备份 {len(jars)} 个 mod 到 {bak_dir}")
        return bak_dir

    os.makedirs(bak_dir, exist_ok=True)
    for f in jars:
        shutil.copy2(os.path.join(MODS_DIR, f), os.path.join(bak_dir, f))
    log(f"已备份 {len(jars)} 个 mod 到 {bak_dir}")
    return bak_dir


# ─────────────────────── Config 合并 ───────────────────────

def parse_cfg_structure(lines):
    """
    解析 Forge .cfg 文件结构。
    返回:
      settings: {(section_path_tuple, key_str): line_index}
      section_closes: {section_path_str: line_index_of_closing_brace}
      section_ranges: {section_path_str: (start_line, end_line)}
    """
    settings = {}
    section_closes = {}
    section_ranges = {}
    stack = []

    for i, line in enumerate(lines):
        s = line.strip()

        # 跳过注释和空行
        if not s or s.startswith("#") or s.startswith("//"):
            continue

        # 段落开始: "sectionname {" 或 独立的 "{"
        if s.endswith("{") and "=" not in s:
            sec_name = s[:-1].strip().strip('"').strip("'")
            if not sec_name:
                # 回溯查找段落名
                for j in range(i - 1, max(i - 5, -1), -1):
                    prev = lines[j].strip()
                    if prev and not prev.startswith("#") and not prev.startswith("//"):
                        sec_name = prev.strip('"').strip("'")
                        break
            stack.append(sec_name)
            path_str = "/".join(stack)
            section_ranges[path_str] = [i, None]
            continue

        # 段落结束
        if s == "}":
            if stack:
                path_str = "/".join(stack)
                if path_str in section_ranges:
                    section_ranges[path_str][1] = i
                section_closes[path_str] = i
                stack.pop()
            continue

        # 设置行: B:key=value, S:key=value, I:key=value 等
        m = re.match(r"([BSIDL]):(.+?)=", s)
        if m:
            key = m.group(2).strip().strip('"')
            sec_tuple = tuple(stack)
            settings[(sec_tuple, key)] = i

    return settings, section_closes, section_ranges


def merge_cfg_content(old_text, new_text):
    """
    合并 Forge .cfg 配置文件。
    保留旧文件中所有已有设置的值，仅添加新设置项。
    返回合并后的文本。
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    old_settings, old_closes, old_ranges = parse_cfg_structure(old_lines)
    new_settings, new_closes, new_ranges = parse_cfg_structure(new_lines)

    # 找出新文件中有、旧文件中没有的设置
    old_keys = set(old_settings.keys())
    new_only_keys = {k: v for k, v in new_settings.items() if k not in old_keys}

    if not new_only_keys:
        return old_text  # 没有新设置

    # 按段落分组
    by_section = {}
    for (sec_tuple, key), line_idx in new_only_keys.items():
        sec_str = "/".join(sec_tuple) if sec_tuple else ""
        by_section.setdefault(sec_str, []).append((key, line_idx))

    insertions = []  # [(insert_before_line, [lines_to_insert])]
    added_sections = set()

    for sec_str, keys in by_section.items():
        keys.sort(key=lambda x: x[1])

        if sec_str in old_closes:
            # 段落在旧文件中存在 → 在其 } 之前插入
            insert_at = old_closes[sec_str]
            block = []
            for key, idx in keys:
                # 收集设置行及其上方的注释
                j = idx - 1
                while j >= 0 and new_lines[j].strip().startswith("#"):
                    j -= 1
                block.extend(new_lines[j + 1 : idx + 1])
            insertions.append((insert_at, block))

        elif sec_str and sec_str not in added_sections:
            # 全新段落 → 从新文件中复制整个段落块
            if sec_str in new_ranges and new_ranges[sec_str][1] is not None:
                start, end = new_ranges[sec_str]
                # 向上收集注释/装饰行
                j = start - 1
                while j >= 0:
                    ls = new_lines[j].strip()
                    if ls.startswith("#") or ls.startswith("~") or ls == "":
                        j -= 1
                    else:
                        break
                j += 1
                block = ["\n"] + new_lines[j : end + 1]
                if not block[-1].endswith("\n"):
                    block.append("\n")

                # 找到插入位置：父段落的 } 之前，或文件末尾
                parent_str = "/".join(sec_str.split("/")[:-1])
                if parent_str and parent_str in old_closes:
                    insert_at = old_closes[parent_str]
                else:
                    insert_at = len(old_lines)
                insertions.append((insert_at, block))
                added_sections.add(sec_str)

        else:
            # 根级新设置 → 追加到文件末尾
            block = []
            for key, idx in keys:
                j = idx - 1
                while j >= 0 and new_lines[j].strip().startswith("#"):
                    j -= 1
                block.extend(new_lines[j + 1 : idx + 1])
            insertions.append((len(old_lines), block))

    # 按位置倒序插入，保持索引正确
    insertions.sort(key=lambda x: x[0], reverse=True)
    result = list(old_lines)
    for pos, block in insertions:
        for line in reversed(block):
            result.insert(pos, line)

    return "".join(result)


def update_configs(inner_zip, dry_run=False):
    """
    合并新 config 设置到现有 config 文件中。
    只处理 .cfg / .conf / .properties 文件。
    """
    cfg_prefix = "config/"
    stats = {"new": 0, "merged": 0, "unchanged": 0, "error": 0}
    merge_details = []

    for entry in inner_zip.namelist():
        if not entry.startswith(cfg_prefix) or entry.endswith("/"):
            continue

        rel = entry[len(cfg_prefix):]

        # 只处理文本配置文件
        ext = os.path.splitext(rel)[1].lower()
        if ext not in (".cfg", ".conf", ".properties"):
            continue

        local = os.path.join(CONFIG_DIR, rel.replace("/", os.sep))

        if not os.path.exists(local):
            # 全新配置文件
            if not dry_run:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with open(local, "wb") as f:
                    f.write(inner_zip.read(entry))
            stats["new"] += 1
            merge_details.append(f"  NEW: {rel}")
            continue

        try:
            new_text = inner_zip.read(entry).decode("utf-8", errors="replace")
            with open(local, "r", encoding="utf-8", errors="replace") as f:
                old_text = f.read()

            if old_text == new_text:
                stats["unchanged"] += 1
                continue

            merged = merge_cfg_content(old_text, new_text)
            if merged != old_text:
                if not dry_run:
                    with open(local, "w", encoding="utf-8", newline="") as f:
                        f.write(merged)
                stats["merged"] += 1
                merge_details.append(f"  MERGED: {rel}")
            else:
                stats["unchanged"] += 1
        except Exception as e:
            stats["error"] += 1
            merge_details.append(f"  ERROR: {rel} - {e}")

    for d in merge_details:
        log(d)
    log(
        f"Config 统计: {stats['new']} 新增, {stats['merged']} 合并, "
        f"{stats['unchanged']} 无变化, {stats['error']} 错误"
    )


# ─────────────────────── 主流程 ───────────────────────

def main():
    parser = argparse.ArgumentParser(description="GTNH Daily Build Server Updater")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不做修改")
    parser.add_argument("--zip", type=str, default=None, help="指定 zip 文件路径")
    args = parser.parse_args()

    dry_run = args.dry_run

    print("=" * 60)
    print("     GTNH Daily Build Server Mod/Config 更新工具")
    print("=" * 60)
    log(f"工作目录: {SCRIPT_DIR}")
    log(f"下载目录: {DOWNLOADS_DIR}")
    if dry_run:
        log("模式: DRY-RUN（仅预览）", "WARN")
    print()

    # ── 查找 zip ──
    if args.zip:
        zip_path = args.zip
        if not os.path.isfile(zip_path):
            log(f"指定的文件不存在: {zip_path}", "ERROR")
            sys.exit(1)
    else:
        zip_path = find_latest_daily_zip([DOWNLOADS_DIR, SCRIPT_DIR])
        if not zip_path:
            log("未找到 gtnh-*-server-*.zip 服务端压缩包！", "ERROR")
            log(f"已搜索: {DOWNLOADS_DIR}, {SCRIPT_DIR}")
            sys.exit(1)

    log(f"使用: {os.path.basename(zip_path)}")

    # ── 解析构建信息 ──
    build_type, build_number = parse_build_info(zip_path)
    if build_type and build_number:
        log(f"构建类型: {build_type}, 构建号: {build_number}")
    else:
        log("无法从文件名解析构建信息，将跳过 motd 更新", "WARN")

    # ── 打开 zip ──
    log("正在读取压缩包...")
    inner = open_inner_zip(zip_path)

    # ── 获取新 mod 列表 ──
    mod_prefix = "mods/"
    new_mods = {}
    for n in inner.namelist():
        if n.startswith(mod_prefix) and n.endswith(".jar"):
            fname = n[len(mod_prefix):]
            if fname:
                new_mods[fname] = n

    current_mods = [f for f in os.listdir(MODS_DIR) if f.endswith(".jar")]
    log(f"当前 mod: {len(current_mods)} 个, 新包 mod: {len(new_mods)} 个")

    # ── 匹配 ──
    matches = match_mods(current_mods, list(new_mods.keys()))

    # ── 加载新增排除列表 ──
    exclude_add = load_exclude_add_list()
    excluded = []
    filtered_matches = []
    for a, c, n in matches:
        if a == "add" and normalize_mod_name(n) in exclude_add:
            excluded.append(n)
        else:
            filtered_matches.append((a, c, n))
    matches = filtered_matches

    updates = [(a, c, n) for a, c, n in matches if a == "update"]
    adds = [(a, c, n) for a, c, n in matches if a == "add"]
    keeps = [(a, c, n) for a, c, n in matches if a == "keep"]
    extras = [(a, c, n) for a, c, n in matches if a == "extra"]

    print()
    log(f"匹配结果: {len(updates)} 更新, {len(adds)} 新增, "
        f"{len(keeps)} 不变, {len(extras)} 用户自添加"
        + (f", {len(excluded)} 排除新增" if excluded else ""))

    # ── 显示更新详情 ──
    if updates:
        print(f"\n  {'─' * 50}")
        print("  更新的 mod:")
        print(f"  {'─' * 50}")
        for _, cur, new in sorted(updates, key=lambda x: x[1].lower()):
            newer = is_newer_version(new, cur)
            tag = "↑ 升级" if newer else "↓ 降级" if not newer else "= 相同"
            print(f"    {cur}")
            print(f"      → {new}  ({tag})")

    if adds:
        print(f"\n  {'─' * 50}")
        print("  新增的 mod:")
        print(f"  {'─' * 50}")
        for _, _, new in sorted(adds, key=lambda x: x[2].lower()):
            print(f"    + {new}")

    if extras:
        print(f"\n  {'─' * 50}")
        print("  用户自添加的 mod（保留不动）:")
        print(f"  {'─' * 50}")
        for _, cur, _ in sorted(extras, key=lambda x: x[1].lower()):
            print(f"    * {cur}")

    if excluded:
        print(f"\n  {'─' * 50}")
        print("  排除新增的 mod（配置排除）:")
        print(f"  {'─' * 50}")
        for name in sorted(excluded, key=str.lower):
            print(f"    - {name}")

    if build_type and build_number:
        print(f"\n  {'─' * 50}")
        print(f"  motd 将更新为: GT\\:New Horizons {build_type} {build_number}")
        print(f"  {'─' * 50}")

    # ── 确认 ──
    print()
    if dry_run:
        log("[DRY-RUN] 预览结束，未做任何修改。")
        sys.exit(0)

    try:
        confirm = input("是否执行更新? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        log("已取消。")
        sys.exit(0)

    if confirm != "y":
        log("已取消。")
        sys.exit(0)

    # ── 备份 ──
    print()
    log("━━━ 备份 mod ━━━")
    backup_mods(dry_run=False)

    # ── 更新 mod ──
    print()
    log("━━━ 更新 mod ━━━")
    update_count = 0
    add_count = 0

    for action, cur, new in matches:
        if action == "keep" or action == "extra":
            continue

        if action == "add":
            dst = os.path.join(MODS_DIR, new)
            with open(dst, "wb") as f:
                f.write(inner.read(new_mods[new]))
            log(f"  新增: {new}")
            add_count += 1

        elif action == "update":
            old_path = os.path.join(MODS_DIR, cur)
            log(f"  更新: {cur} → {new}")
            os.remove(old_path)
            with open(os.path.join(MODS_DIR, new), "wb") as f:
                f.write(inner.read(new_mods[new]))
            update_count += 1

    log(f"Mod 更新完成: {update_count} 更新, {add_count} 新增")

    # ── 更新 config ──
    print()
    log("━━━ 更新 config ━━━")
    update_configs(inner, dry_run=False)

    # ── 更新 motd ──
    if build_type and build_number:
        print()
        log("━━━ 更新 server.properties motd ━━━")
        update_server_motd(build_type, build_number, dry_run=False)

    # ── 完成 ──
    print()
    print("=" * 60)
    log("全部完成！如需恢复，备份在 back/ 目录中。")
    print("=" * 60)


if __name__ == "__main__":
    main()
