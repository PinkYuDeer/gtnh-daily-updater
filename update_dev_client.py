#!/usr/bin/env python3
"""
GTNH Dev Build Client Updater
==============================
对比并更新来自新 daily/experimental build 压缩包的 mods 和 config（客户端）。
将此脚本放置于 .minecraft 目录下运行。

用法:
  python update_dev_client.py              # 自动查找最新的客户端 build
  python update_dev_client.py --dry-run    # 仅预览，不做任何修改
  python update_dev_client.py --zip <path> # 指定压缩包路径

在 update_daily.cfg 中将 [player_package] enabled 设为 true，
可在本地更新后同时生成可发给玩家的离线更新包。
"""

import os
import sys
import re
import shutil
import zipfile
import io
import json
import argparse
import subprocess
import tempfile
from datetime import datetime

# 下载目录（daily build 压缩包所在位置）
DOWNLOADS_DIR = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), "Downloads"
)


def get_application_dir():
    """返回脚本/可执行文件所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SCRIPT_DIR = get_application_dir()
MODS_DIR = os.path.join(SCRIPT_DIR, "mods")
CONFIG_DIR = os.path.join(SCRIPT_DIR, "config")
BACKUP_DIR = os.path.join(SCRIPT_DIR, "back")
UPDATE_CFG_PATH = os.path.join(SCRIPT_DIR, "update_daily.cfg")
PLAYER_PACKAGES_DIR = os.path.join(SCRIPT_DIR, "player_updates")
PLAYER_PAYLOAD_PREFIX = "gtnh-player-update-"
PLAYER_MANIFEST_NAME = "player_update_manifest.json"
PLAYER_UPDATER_EXE_NAME = "update_dev_client.exe"


# ─────────────────────── 工具函数 ───────────────────────

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def load_exclude_mod_list():
    """
    从 update_daily.cfg 加载 mod 排除列表。
    配置文件格式: [exclude] 段下每行一个 mod 名称（normalize 后匹配）。
    为兼容旧配置，也会读取 [exclude_add] 段。
    若文件不存在则自动创建默认配置。
    """
    default_content = """\
# update_daily.cfg — 更新工具配置文件
# 此文件控制更新脚本的行为。

[exclude]
# 以下 mod 将被排除（不会自动新增或更新），本地已有文件会保留不动。
# 每行一个 mod 的标准化名称（不含版本号，大小写不敏感）。
# 示例:
# SomeMod
# AnotherMod

[player_package]
# 本地更新完成后，是否同时生成可供玩家使用的离线更新包。
# 生成的外层 zip 包含本次差量资源 zip 和 update_dev_client.exe。
enabled = false
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
                in_section = line.lower() in ("[exclude]", "[exclude_add]")
                continue
            if in_section and line and not line.startswith("#"):
                exclude.add(line.lower())
    return exclude


def load_player_package_enabled():
    """读取 [player_package] enabled，缺省或配置无效时均为 False。"""
    if not os.path.exists(UPDATE_CFG_PATH):
        # 复用默认配置的创建逻辑。
        load_exclude_mod_list()

    in_section = False
    value = None
    with open(UPDATE_CFG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                in_section = line.lower() == "[player_package]"
                continue
            if not in_section or not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, raw_value = (part.strip().lower() for part in line.split("=", 1))
            if key == "enabled":
                value = raw_value

    if value is None:
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    if value not in ("0", "false", "no", "off"):
        log(
            "[player_package] enabled 配置无效，"
            "请使用 true/false；本次按 false 处理",
            "WARN",
        )
    return False


def is_excluded_mod(action, cur_file, new_file, exclude_mods):
    """判断一次 add/update 是否应该被配置排除。"""
    if action not in ("add", "update"):
        return False

    candidates = []
    if cur_file:
        candidates.append(normalize_mod_name(cur_file))
    if new_file:
        candidates.append(normalize_mod_name(new_file))
    return any(name in exclude_mods for name in candidates)


def find_latest_daily_zip(search_dirs):
    """
    在给定的目录列表中查找最新的 gtnh-*-mmcprism-*.zip（客户端包）。
    支持 daily / experimental 等构建类型，以及 java8 / new-java 变体。
    按目录优先级依次搜索，找到即返回。
    """
    pat = re.compile(
        r"gtnh-\w+-(\d{4}-\d{2}-\d{2})\+(\d+)-mmcprism[-\w]*\.zip$", re.I
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


def is_player_update_payload(zip_path):
    """判断是否为本工具生成的玩家差量包。"""
    if not os.path.isfile(zip_path) or not zipfile.is_zipfile(zip_path):
        return False
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return PLAYER_MANIFEST_NAME in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def find_bundled_player_payload(directory):
    """查找与玩家更新 exe 放在同一目录的差量包。"""
    if not os.path.isdir(directory):
        return None

    candidates = []
    for name in os.listdir(directory):
        if not name.lower().startswith(PLAYER_PAYLOAD_PREFIX):
            continue
        if not name.lower().endswith(".zip"):
            continue
        path = os.path.join(directory, name)
        if is_player_update_payload(path):
            candidates.append(path)

    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def zip_has_direct_content(zf):
    """判断 zip 是否已经直接包含实例内容，而不是只包含内层 zip。"""
    markers = (
        "mods/",
        ".minecraft/mods/",
        "config/",
        ".minecraft/config/",
        "libraries/",
    )
    for name in zf.namelist():
        normalized = name.replace("\\", "/")
        if any(
            normalized.startswith(marker) or f"/{marker}" in normalized
            for marker in markers
        ):
            return True
    return False


def open_inner_zip(outer_path):
    """
    打开构建 zip，返回可直接读取内容的 ZipFile 对象。
    兼容旧格式（外层 artifact 内含 GTNH zip）和新格式（单层 zip）。
    """
    outer = zipfile.ZipFile(outer_path, "r")
    if zip_has_direct_content(outer):
        log("检测到压缩包已直接包含内容，按单层 zip 处理")
        return outer

    inner_name = next(
        (
            n for n in outer.namelist()
            if n.lower().endswith(".zip")
            and os.path.basename(n).lower().startswith("gtnh")
        ),
        None,
    )
    if not inner_name:
        outer.close()
        raise FileNotFoundError("压缩包未直接包含可用内容，且未找到内层 GTNH zip")

    data = outer.read(inner_name)
    outer.close()
    log(f"内层 zip: {inner_name}")
    return zipfile.ZipFile(io.BytesIO(data))


def detect_section_prefix(zf, section_path, required_ext=None):
    """
    检测 zip 内某个目录段的前缀。
    例如 mods/xxx.jar -> "", GTNH/.minecraft/mods/xxx.jar -> "GTNH/"。
    """
    section = section_path.strip("/")
    marker = f"/{section}/"
    names = []
    for name in zf.namelist():
        normalized = name.replace("\\", "/")
        if normalized.endswith("/"):
            continue
        if required_ext and not normalized.lower().endswith(required_ext):
            continue
        names.append(normalized)

    # 先查找根级目录，避免被 journeymap/config 等嵌套路径抢先匹配。
    for normalized in names:
        if normalized.startswith(f"{section}/"):
            return ""

    for normalized in names:
        idx = normalized.find(marker)
        if idx != -1:
            return normalized[: idx + 1]
    return None


def detect_mod_prefix(zf):
    """检测 mods 目录前缀，兼容 .minecraft/mods 和根目录 mods。"""
    prefix = detect_section_prefix(zf, ".minecraft/mods", ".jar")
    if prefix is not None:
        return f"{prefix}.minecraft/mods/"

    prefix = detect_section_prefix(zf, "mods", ".jar")
    if prefix is not None:
        return f"{prefix}mods/"
    return None


def detect_config_prefix(zf):
    """检测 config 目录前缀，兼容 .minecraft/config 和根目录 config。"""
    prefix = detect_section_prefix(zf, ".minecraft/config")
    if prefix is not None:
        return f"{prefix}.minecraft/config/"

    prefix = detect_section_prefix(zf, "config")
    if prefix is not None:
        return f"{prefix}config/"
    return None


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


def compare_mod_versions(new_file, old_file):
    """比较两个 mod 文件名中的版本，返回 1 / 0 / -1。"""
    _, new_v = extract_version_parts(new_file)
    _, old_v = extract_version_parts(old_file)
    if new_v > old_v:
        return 1
    if new_v < old_v:
        return -1
    return 0


def compare_display_versions(new_value, old_value):
    """比较裸版本号字符串，返回 1 / 0 / -1。"""
    _, new_v = extract_version_parts(f"version-{display_version(new_value)}.jar")
    _, old_v = extract_version_parts(f"version-{display_version(old_value)}.jar")
    if new_v > old_v:
        return 1
    if new_v < old_v:
        return -1
    return 0


def version_change_tag(compare_result):
    """返回与 mod 更新预览一致的版本变化标签。"""
    if compare_result > 0:
        return "↑ 升级"
    if compare_result < 0:
        return "↓ 降级"
    return "= 相同"


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


# ─────────────────────── Prism Libraries 更新 ───────────────────────

def get_prism_instance_dir():
    """
    检测当前脚本是否位于 Prism/MultiMC 实例的 .minecraft 目录中。
    Prism/MultiMC 结构通常是:
      instance/
      ├── libraries/
      ├── mmc-pack.json / instance.cfg
      └── .minecraft/
          └── update_dev_client.py
    """
    instance_dir = os.path.dirname(SCRIPT_DIR)
    libraries_dir = os.path.join(instance_dir, "libraries")
    prism_markers = (
        os.path.join(instance_dir, "mmc-pack.json"),
        os.path.join(instance_dir, "instance.cfg"),
    )
    if os.path.isdir(libraries_dir) or any(os.path.isfile(p) for p in prism_markers):
        return instance_dir
    return None


def safe_join(root, rel_path):
    """安全拼接 zip 内相对路径，避免写出目标目录。"""
    parts = [
        p for p in rel_path.replace("\\", "/").split("/")
        if p and p not in (".", "..")
    ]
    path = os.path.abspath(os.path.join(root, *parts))
    root_abs = os.path.abspath(root)
    if os.path.commonpath([root_abs, path]) != root_abs:
        raise ValueError(f"非法路径: {rel_path}")
    return path


def detect_instance_root_prefix(inner_zip):
    """
    检测压缩包内实例根目录前缀。
    例如:
      libraries/xxx                       -> ""
      GTNH-Client/libraries/xxx           -> "GTNH-Client/"
      GTNH-Client/.minecraft/mods/xxx.jar -> "GTNH-Client/"
    """
    for name in inner_zip.namelist():
        normalized = name.replace("\\", "/")
        if normalized.startswith("libraries/"):
            return ""
        marker = "/libraries/"
        idx = normalized.find(marker)
        if idx != -1:
            return normalized[: idx + 1]

    for name in inner_zip.namelist():
        normalized = name.replace("\\", "/")
        if normalized.startswith(".minecraft/"):
            return ""
        marker = "/.minecraft/"
        idx = normalized.find(marker)
        if idx != -1:
            return normalized[: idx + 1]
    return ""


def collect_library_entries(inner_zip, root_prefix):
    """收集压缩包中的 libraries 文件。"""
    prefix = f"{root_prefix}libraries/"
    entries = []
    for entry in inner_zip.namelist():
        normalized = entry.replace("\\", "/")
        if not normalized.startswith(prefix) or normalized.endswith("/"):
            continue
        rel = normalized[len(prefix):]
        if rel:
            entries.append((entry, rel))
    return entries


def collect_old_lwjgl3ify_paths(libraries_dir):
    """收集本地 libraries 下旧的 lwjgl3ify 文件或目录。"""
    candidates = []
    for root, dirs, files in os.walk(libraries_dir):
        for d in dirs:
            path = os.path.join(root, d)
            rel = os.path.relpath(path, libraries_dir).replace(os.sep, "/")
            if "lwjgl3ify" in rel.lower():
                candidates.append(path)
        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, libraries_dir).replace(os.sep, "/")
            if "lwjgl3ify" in rel.lower():
                candidates.append(path)

    selected = []
    for path in sorted(candidates, key=lambda p: len(os.path.abspath(p))):
        path_abs = os.path.abspath(path)
        if any(
            os.path.commonpath([os.path.abspath(parent), path_abs])
            == os.path.abspath(parent)
            for parent in selected
        ):
            continue
        selected.append(path)
    return selected


def read_json_from_zip(inner_zip, entry):
    """从 zip 读取 JSON，失败时返回 None。"""
    try:
        return json.loads(inner_zip.read(entry).decode("utf-8-sig"))
    except Exception:
        return None


def component_key(component):
    if not isinstance(component, dict):
        return ""
    return str(
        component.get("uid")
        or component.get("name")
        or component.get("id")
        or ""
    )


def is_lwjgl3ify_component(component):
    key = component_key(component).lower()
    return "lwjgl3ify" in key


def collect_pack_json_sources(inner_zip, root_prefix):
    """
    收集压缩包实例根目录下的 pack JSON。
    只考虑根目录 json，避免误读 .minecraft/config 下的普通配置。
    """
    sources = []
    for entry in inner_zip.namelist():
        normalized = entry.replace("\\", "/")
        if not normalized.startswith(root_prefix) or normalized.endswith("/"):
            continue
        rel = normalized[len(root_prefix):]
        if "/" in rel or not rel.lower().endswith(".json"):
            continue

        data = read_json_from_zip(inner_zip, entry)
        if not isinstance(data, dict):
            continue
        components = data.get("components")
        if rel.lower() == "mmc-pack.json" or isinstance(components, list):
            sources.append((entry, rel, data))
    return sources


def local_pack_json_candidates(instance_dir):
    """查找实例根目录下可能需要更新版本号的 JSON。"""
    candidates = []
    mmc_pack = os.path.join(instance_dir, "mmc-pack.json")
    if os.path.isfile(mmc_pack):
        candidates.append(mmc_pack)

    for name in os.listdir(instance_dir):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(instance_dir, name)
        if os.path.isfile(path) and path not in candidates:
            candidates.append(path)
    return candidates


def source_lwjgl3ify_components(pack_sources):
    """从新包 pack JSON 中提取 lwjgl3ify 组件版本。"""
    sources = {}
    for _, _, data in pack_sources:
        components = data.get("components")
        if isinstance(components, list):
            for component in components:
                key = component_key(component)
                if key and is_lwjgl3ify_component(component):
                    sources[key] = component
        key = component_key(data)
        if key and is_lwjgl3ify_component(data):
            sources[key] = data
    return sources


def apply_lwjgl3ify_versions(data, source_components):
    """
    将新包中的 lwjgl3ify 版本写入本地 pack JSON 数据。
    返回 (是否变化, 描述列表)。
    """
    changed = False
    details = []

    def update_component(local_component, source_component):
        nonlocal changed
        key = component_key(local_component)
        for field in ("version", "cachedVersion"):
            if field not in source_component:
                continue
            old = local_component.get(field)
            new = source_component[field]
            if old != new:
                local_component[field] = new
                changed = True
                details.append(f"{key or 'lwjgl3ify'} {field}: {old} -> {new}")

    components = data.get("components")
    if isinstance(components, list):
        local_relevant = [
            component for component in components
            if is_lwjgl3ify_component(component)
        ]
        for component in local_relevant:
            key = component_key(component)
            source = source_components.get(key)
            if source:
                update_component(component, source)

        if not local_relevant and source_components:
            for source in source_components.values():
                components.append(dict(source))
                changed = True
                details.append(f"新增组件: {component_key(source)}")

    key = component_key(data)
    if key and is_lwjgl3ify_component(data) and key in source_components:
        update_component(data, source_components[key])

    return changed, details


def plan_pack_json_updates(instance_dir, pack_sources):
    """计算本地 pack JSON 将发生的版本更新。"""
    source_components = source_lwjgl3ify_components(pack_sources)
    if not source_components:
        return []

    updates = []
    for path in local_pack_json_candidates(instance_dir):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            continue

        updated_data = json.loads(json.dumps(data, ensure_ascii=False))
        changed, details = apply_lwjgl3ify_versions(
            updated_data,
            source_components,
        )
        if changed:
            updates.append({
                "path": path,
                "data": updated_data,
                "details": details,
            })
    return updates


def display_version(value):
    """格式化版本号显示。"""
    if value is None or value == "" or value == "None":
        return "未知"
    return str(value)


def collect_lwjgl3ify_version_changes(pack_updates):
    """从 pack JSON 更新详情中提取去重后的 lwjgl3ify 版本变化。"""
    changes = []
    seen = set()
    version_pat = re.compile(r"^(.+?) version: (.*?) -> (.*)$")

    for item in pack_updates:
        for detail in item.get("details", []):
            m = version_pat.match(detail)
            if not m:
                continue
            old = display_version(m.group(2))
            new = display_version(m.group(3))
            key = ("lwjgl3ify", old, new)
            if key in seen:
                continue
            seen.add(key)
            changes.append(key)
    return changes


def extract_lwjgl3ify_version_from_path(path):
    """从 lwjgl3ify library 路径中尽量提取版本号。"""
    normalized = path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    for i, part in enumerate(parts):
        if "lwjgl3ify" in part.lower() and i + 1 < len(parts):
            return parts[i + 1]

    filename = os.path.basename(normalized)
    m = re.search(r"lwjgl3ify[-_](.+?)(?:\.jar|\.zip)?$", filename, re.I)
    if m:
        return m.group(1)
    return None


def collect_lwjgl3ify_library_change(old_paths, library_entries):
    """pack JSON 没有版本信息时，从 library 路径兜底生成一条变化提示。"""
    old_versions = sorted({
        v for v in (
            extract_lwjgl3ify_version_from_path(path)
            for path in old_paths
        )
        if v
    })
    new_versions = sorted({
        v for v in (
            extract_lwjgl3ify_version_from_path(rel)
            for _, rel in library_entries
            if "lwjgl3ify" in rel.lower()
        )
        if v
    })
    if not old_versions and not new_versions:
        return None
    old = old_versions[-1] if old_versions else "未知"
    new = new_versions[-1] if new_versions else "未知"
    return ("lwjgl3ify", old, new)


def build_prism_libraries_plan(inner_zip):
    """构建 Prism/MultiMC libraries 更新计划。"""
    instance_dir = get_prism_instance_dir()
    if not instance_dir:
        return {"is_prism": False}

    root_prefix = detect_instance_root_prefix(inner_zip)
    library_entries = collect_library_entries(inner_zip, root_prefix)
    libraries_dir = os.path.join(instance_dir, "libraries")
    old_lwjgl3ify_paths = (
        collect_old_lwjgl3ify_paths(libraries_dir)
        if library_entries else []
    )
    pack_sources = collect_pack_json_sources(inner_zip, root_prefix)
    pack_updates = plan_pack_json_updates(instance_dir, pack_sources)
    version_changes = collect_lwjgl3ify_version_changes(pack_updates)
    fallback_change = collect_lwjgl3ify_library_change(
        old_lwjgl3ify_paths,
        library_entries,
    )
    if not version_changes and fallback_change:
        version_changes = [fallback_change]

    return {
        "is_prism": True,
        "instance_dir": instance_dir,
        "libraries_dir": libraries_dir,
        "root_prefix": root_prefix,
        "library_entries": library_entries,
        "old_lwjgl3ify_paths": old_lwjgl3ify_paths,
        "pack_sources": pack_sources,
        "pack_updates": pack_updates,
        "version_changes": version_changes,
    }


def print_prism_libraries_plan(plan):
    """在确认前显示 Prism libraries 更新摘要。"""
    if not plan.get("is_prism"):
        return

    print(f"\n  {'─' * 50}")
    print("  更新的 Prism/MultiMC libraries:")
    print(f"  {'─' * 50}")

    library_entries = plan.get("library_entries", [])
    pack_updates = plan.get("pack_updates", [])
    version_changes = plan.get("version_changes", [])

    if not library_entries and not pack_updates:
        print("    未在新包中发现 libraries 或 lwjgl3ify 版本变更")
        return

    if version_changes:
        for name, old, new in version_changes:
            tag = version_change_tag(compare_display_versions(new, old))
            print(f"    {name}")
            print(f"      {old} → {new}  ({tag})")
    else:
        print("    lwjgl3ify")
        print("      将同步新包中的 libraries")


def backup_prism_path(path, instance_dir, backup_root, backed_up):
    """备份 Prism libraries / pack JSON 相关文件。"""
    if not os.path.exists(path):
        return
    abs_path = os.path.abspath(path)
    if abs_path in backed_up:
        return

    rel = os.path.relpath(abs_path, os.path.abspath(instance_dir))
    dst = os.path.join(backup_root, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(abs_path):
        shutil.copytree(abs_path, dst)
    else:
        shutil.copy2(abs_path, dst)
    backed_up.add(abs_path)


def update_prism_libraries(inner_zip, plan, dry_run=False):
    """按计划更新 Prism/MultiMC 实例 libraries 和 pack JSON。"""
    if not plan.get("is_prism"):
        log("未检测到 Prism/MultiMC libraries 目录，跳过 libraries 更新")
        return

    library_entries = plan.get("library_entries", [])
    pack_updates = plan.get("pack_updates", [])
    old_paths = plan.get("old_lwjgl3ify_paths", [])

    if not library_entries and not pack_updates:
        log("新包中未发现 Prism libraries 或 lwjgl3ify 版本变更，跳过")
        return

    instance_dir = plan["instance_dir"]
    libraries_dir = plan["libraries_dir"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = os.path.join(BACKUP_DIR, f"prism_{ts}")
    backed_up = set()

    if dry_run:
        log(f"[DRY-RUN] 将移除旧 lwjgl3ify {len(old_paths)} 项")
        log(f"[DRY-RUN] 将写入 libraries 文件 {len(library_entries)} 个")
        log(f"[DRY-RUN] 将更新 pack JSON {len(pack_updates)} 个")
        return

    for path in old_paths:
        if not os.path.exists(path):
            continue
        backup_prism_path(path, instance_dir, backup_root, backed_up)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        log(f"  移除旧 lwjgl3ify: {os.path.relpath(path, libraries_dir)}")

    write_count = 0
    for entry, rel in library_entries:
        dst = safe_join(libraries_dir, rel)
        if os.path.exists(dst):
            backup_prism_path(dst, instance_dir, backup_root, backed_up)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(inner_zip.read(entry))
        write_count += 1

    for item in pack_updates:
        backup_prism_path(item["path"], instance_dir, backup_root, backed_up)
        with open(item["path"], "w", encoding="utf-8") as f:
            json.dump(item["data"], f, ensure_ascii=False, indent=2)
            f.write("\n")
        log(f"  更新 pack JSON: {os.path.basename(item['path'])}")
        for detail in item["details"]:
            log(f"    {detail}")

    log(
        f"Prism libraries 更新完成: {write_count} 个文件, "
        f"{len(pack_updates)} 个 pack JSON"
    )
    if backed_up:
        log(f"Prism 相关备份: {backup_root}")


# ─────────────────────── 玩家离线更新包 ───────────────────────

def normalize_archive_rel_path(rel_path):
    """将相对路径转为安全的 zip 路径。"""
    normalized = rel_path.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or any(part in ("", ".", "..") for part in parts)
        or any(":" in part for part in parts)
    ):
        raise ValueError(f"非法的压缩包路径: {rel_path}")
    return "/".join(parts)


def build_player_package_plan(inner_zip, matches, new_mods, cfg_prefix):
    """收集玩家差量包需要写入的 mod/config/libraries。"""
    mod_changes = []
    seen_mods = set()
    for action, cur, new in matches:
        if action not in ("add", "update") or not new or new in seen_mods:
            continue
        entry = new_mods.get(new)
        if not entry:
            continue
        seen_mods.add(new)
        mod_changes.append({
            "action": action,
            "old": cur,
            "new": new,
            "entry": entry,
        })

    config_entries = []
    if cfg_prefix:
        for entry in inner_zip.namelist():
            normalized = entry.replace("\\", "/")
            if not normalized.startswith(cfg_prefix) or normalized.endswith("/"):
                continue
            rel = normalized[len(cfg_prefix):]
            if os.path.splitext(rel)[1].lower() in (".cfg", ".conf", ".properties"):
                config_entries.append((entry, normalize_archive_rel_path(rel)))

    root_prefix = detect_instance_root_prefix(inner_zip)
    library_entries = [
        (entry, normalize_archive_rel_path(rel))
        for entry, rel in collect_library_entries(inner_zip, root_prefix)
    ]
    pack_sources = collect_pack_json_sources(inner_zip, root_prefix)

    return {
        "mod_changes": mod_changes,
        "config_entries": config_entries,
        "library_entries": library_entries,
        "pack_sources": pack_sources,
    }


def player_package_source_tag(zip_path):
    """从原构建包名生成稳定、可用于文件名的标识。"""
    stem = os.path.splitext(os.path.basename(zip_path))[0]
    tag = re.sub(r"[^A-Za-z0-9._+-]+", "-", stem).strip("-._")
    return tag or datetime.now().strftime("%Y%m%d-%H%M%S")


def write_player_payload(payload_path, source_zip_path, inner_zip, package_plan):
    """生成仅包含本次需要下发资源的内层 zip。"""
    mod_changes = package_plan["mod_changes"]
    config_entries = package_plan["config_entries"]
    library_entries = package_plan["library_entries"]
    pack_sources = package_plan["pack_sources"]

    manifest = {
        "format": 1,
        "type": "gtnh-player-update",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_archive": os.path.basename(source_zip_path),
        "mod_changes": [
            {
                "action": item["action"],
                "old": item["old"],
                "new": item["new"],
            }
            for item in mod_changes
        ],
        "config_files": [rel for _, rel in config_entries],
        "library_files": [rel for _, rel in library_entries],
        "pack_json_files": [rel for _, rel, _ in pack_sources],
    }

    written = set()
    with zipfile.ZipFile(payload_path, "w", zipfile.ZIP_DEFLATED) as payload:
        for item in mod_changes:
            arcname = "mods/" + normalize_archive_rel_path(item["new"])
            if arcname in written:
                continue
            payload.writestr(arcname, inner_zip.read(item["entry"]))
            written.add(arcname)

        for entry, rel in config_entries:
            arcname = "config/" + rel
            if arcname in written:
                continue
            payload.writestr(arcname, inner_zip.read(entry))
            written.add(arcname)

        for entry, rel in library_entries:
            arcname = "libraries/" + rel
            if arcname in written:
                continue
            payload.writestr(arcname, inner_zip.read(entry))
            written.add(arcname)

        for entry, rel, _ in pack_sources:
            arcname = normalize_archive_rel_path(rel)
            if arcname in written:
                continue
            payload.writestr(arcname, inner_zip.read(entry))
            written.add(arcname)

        payload.writestr(
            PLAYER_MANIFEST_NAME,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    return manifest


def build_player_updater_exe(temp_dir):
    """返回当前版本客户端更新器 exe，脚本模式下使用 PyInstaller 构建。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)

    if os.name != "nt":
        raise RuntimeError("玩家更新器 exe 需要在 Windows 上构建")

    source_path = os.path.abspath(__file__)
    dist_dir = os.path.join(temp_dir, "dist")
    work_dir = os.path.join(temp_dir, "work")
    spec_dir = os.path.join(temp_dir, "spec")
    for directory in (dist_dir, work_dir, spec_dir):
        os.makedirs(directory, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name", os.path.splitext(PLAYER_UPDATER_EXE_NAME)[0],
        "--distpath", dist_dir,
        "--workpath", work_dir,
        "--specpath", spec_dir,
        source_path,
    ]
    exe_path = os.path.join(dist_dir, PLAYER_UPDATER_EXE_NAME)
    log(f"使用当前 Python 构建 exe: {sys.executable}")
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    if result.returncode == 0 and os.path.isfile(exe_path):
        return exe_path

    output_tail = (result.stdout or "")[-2000:].strip()
    raise RuntimeError(
        "无法使用当前 Python 的 PyInstaller 构建 "
        f"{PLAYER_UPDATER_EXE_NAME}。\n当前 Python: {sys.executable}"
        "\n请使用同一个 Python 执行: "
        "python -m pip install pyinstaller"
        + (f"\n\n{output_tail}" if output_tail else "")
    )


def create_player_distribution_package(
    source_zip_path,
    inner_zip,
    package_plan,
    output_dir=None,
):
    """生成可直接解压到 .minecraft 的外层玩家更新包。"""
    if output_dir is None:
        output_dir = PLAYER_PACKAGES_DIR
    os.makedirs(output_dir, exist_ok=True)

    tag = player_package_source_tag(source_zip_path)
    payload_name = f"{PLAYER_PAYLOAD_PREFIX}{tag}.zip"
    package_name = f"GTNH-player-package-{tag}.zip"
    final_path = os.path.join(output_dir, package_name)

    with tempfile.TemporaryDirectory(prefix="gtnh-player-package-") as temp_dir:
        payload_path = os.path.join(temp_dir, payload_name)
        manifest = write_player_payload(
            payload_path,
            source_zip_path,
            inner_zip,
            package_plan,
        )
        exe_path = build_player_updater_exe(temp_dir)
        if not os.path.isfile(exe_path):
            raise FileNotFoundError(f"更新器 exe 不存在: {exe_path}")

        temp_package = os.path.join(temp_dir, package_name)
        # 内层 payload 和 exe 已经压缩，外层直接存储可避免二次压缩耗时。
        with zipfile.ZipFile(temp_package, "w", zipfile.ZIP_STORED) as package:
            package.write(payload_path, payload_name)
            package.write(exe_path, PLAYER_UPDATER_EXE_NAME)
        os.replace(temp_package, final_path)

    return final_path, manifest


def print_player_package_plan(package_plan):
    """显示额外玩家包的资源摘要。"""
    print(f"\n  {'─' * 50}")
    print("  玩家离线更新包: 已启用")
    print(f"  {'─' * 50}")
    print(f"    差量 mod: {len(package_plan['mod_changes'])} 个")
    print(f"    config 更新源: {len(package_plan['config_entries'])} 个")
    print(f"    Prism libraries: {len(package_plan['library_entries'])} 个")
    print(f"    Prism pack JSON: {len(package_plan['pack_sources'])} 个")
    print(f"    输出目录: {PLAYER_PACKAGES_DIR}")


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


def update_configs(inner_zip, cfg_prefix=None, dry_run=False):
    """
    合并新 config 设置到现有 config 文件中。
    只处理 .cfg / .conf / .properties 文件。
    """
    if cfg_prefix is None:
        cfg_prefix = detect_config_prefix(inner_zip)

    if not cfg_prefix:
        log("zip 中未找到 config 目录", "WARN")
        return

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
    parser = argparse.ArgumentParser(description="GTNH Dev Build Client Updater")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不做修改")
    parser.add_argument("--zip", type=str, default=None, help="指定 zip 文件路径")
    args = parser.parse_args()

    dry_run = args.dry_run

    print("=" * 60)
    print("     GTNH Dev Build Client Mod/Config 更新工具")
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
        # 玩家双击外层包中的 exe 时，优先使用与 exe 同目录的差量包。
        zip_path = None
        if getattr(sys, "frozen", False):
            zip_path = find_bundled_player_payload(SCRIPT_DIR)
            if zip_path:
                log("已检测到玩家离线更新包")
        if not zip_path:
            zip_path = find_latest_daily_zip([DOWNLOADS_DIR, SCRIPT_DIR])
        if not zip_path:
            log("未找到 gtnh-*-mmcprism-*.zip 客户端压缩包！", "ERROR")
            log(f"已搜索: {DOWNLOADS_DIR}, {SCRIPT_DIR}")
            sys.exit(1)

    log(f"使用: {os.path.basename(zip_path)}")
    player_payload_mode = is_player_update_payload(zip_path)
    if player_payload_mode:
        log("模式: 玩家差量更新")

    # ── 打开 zip ──
    log("正在读取压缩包...")
    inner = open_inner_zip(zip_path)

    # ── 获取新 mod 列表 ──
    mod_prefix = detect_mod_prefix(inner)

    if not mod_prefix:
        if player_payload_mode:
            log("本次玩家差量包不包含 mod 变更")
        else:
            log("zip 中未找到 mods 目录！", "ERROR")
            sys.exit(1)
    else:
        log(f"mods 前缀: {mod_prefix}")

    cfg_prefix = detect_config_prefix(inner)
    if cfg_prefix:
        log(f"config 前缀: {cfg_prefix}")
    else:
        log("zip 中未找到 config 目录，将跳过 config 更新", "WARN")

    new_mods = {}
    if mod_prefix:
        for n in inner.namelist():
            if n.startswith(mod_prefix) and n.endswith(".jar"):
                fname = n[len(mod_prefix):]
                if fname:
                    new_mods[fname] = n

    current_mods = [f for f in os.listdir(MODS_DIR) if f.endswith(".jar")]
    log(f"当前 mod: {len(current_mods)} 个, 新包 mod: {len(new_mods)} 个")

    # ── 匹配 ──
    matches = match_mods(current_mods, list(new_mods.keys()))

    # ── 加载 mod 排除列表 ──
    exclude_mods = load_exclude_mod_list()
    player_package_enabled = load_player_package_enabled()
    if player_payload_mode and player_package_enabled:
        log("当前输入已是玩家差量包，不会再次生成外层包", "WARN")
        player_package_enabled = False
    excluded = []
    filtered_matches = []
    for a, c, n in matches:
        if is_excluded_mod(a, c, n, exclude_mods):
            excluded.append(n or c)
        else:
            filtered_matches.append((a, c, n))
    matches = filtered_matches

    prism_plan = build_prism_libraries_plan(inner)
    player_package_plan = None
    if player_package_enabled:
        player_package_plan = build_player_package_plan(
            inner,
            matches,
            new_mods,
            cfg_prefix,
        )

    updates = [(a, c, n) for a, c, n in matches if a == "update"]
    adds = [(a, c, n) for a, c, n in matches if a == "add"]
    keeps = [(a, c, n) for a, c, n in matches if a == "keep"]
    extras = [(a, c, n) for a, c, n in matches if a == "extra"]

    print()
    log(f"匹配结果: {len(updates)} 更新, {len(adds)} 新增, "
        f"{len(keeps)} 不变, {len(extras)} 用户自添加"
        + (f", {len(excluded)} 配置排除" if excluded else ""))

    # ── 显示更新详情 ──
    if updates:
        print(f"\n  {'─' * 50}")
        print("  更新的 mod:")
        print(f"  {'─' * 50}")
        for _, cur, new in sorted(updates, key=lambda x: x[1].lower()):
            tag = version_change_tag(compare_mod_versions(new, cur))
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
        print("  配置排除的 mod（保留本地文件）:")
        print(f"  {'─' * 50}")
        for name in sorted(excluded, key=str.lower):
            print(f"    - {name}")

    print_prism_libraries_plan(prism_plan)
    if player_package_plan:
        print_player_package_plan(player_package_plan)

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

    # ── 生成玩家离线更新包 ──
    # 必须在修改本地 mod/config 之前完成；否则构建 exe 失败后
    # 再次运行时已无法从更新完成的本地文件还原本次 mod 差量。
    player_package_path = None
    if player_package_plan:
        print()
        log("━━━ 生成玩家离线更新包 ━━━")
        try:
            player_package_path, package_manifest = create_player_distribution_package(
                zip_path,
                inner,
                player_package_plan,
            )
            log(
                f"玩家包已生成: {player_package_path} "
                f"(mod {len(package_manifest['mod_changes'])}, "
                f"config {len(package_manifest['config_files'])}, "
                f"libraries {len(package_manifest['library_files'])})"
            )
        except Exception as exc:
            log(f"玩家包生成失败: {exc}", "ERROR")
            log("为保留本次更新差量，尚未修改本地任何文件", "WARN")
            sys.exit(1)

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

    # ── 更新 Prism/MultiMC libraries ──
    print()
    log("━━━ 更新 Prism/MultiMC libraries ━━━")
    update_prism_libraries(inner, prism_plan, dry_run=False)

    # ── 更新 config ──
    print()
    log("━━━ 更新 config ━━━")
    if cfg_prefix:
        update_configs(inner, cfg_prefix, dry_run=False)

    # ── 完成 ──
    print()
    print("=" * 60)
    log("全部完成！如需恢复，备份在 back/ 目录中。")
    if player_package_path:
        log(f"发给玩家的文件: {player_package_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
