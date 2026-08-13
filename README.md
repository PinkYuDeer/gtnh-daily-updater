# GTNH Dev Build Updater

自动更新 [GT: New Horizons](https://github.com/GTNewHorizons) dev build（daily / experimental）的 mods 和 config 的命令行工具。

支持客户端（MMC/Prism Launcher）和服务端两种场景。

## 前置步骤：下载构建包

本工具**不会**自动从网络下载构建包。使用前，请先从以下 GitHub Actions 页面手动下载对应的压缩包：

- **Daily Build**：[daily-modpack-build.yml](https://github.com/GTNewHorizons/DreamAssemblerXXL/actions/workflows/daily-modpack-build.yml)
- **Experimental Build**：[experimental-modpack-build.yml](https://github.com/GTNewHorizons/DreamAssemblerXXL/actions/workflows/experimental-modpack-build.yml)

在 Actions 页面中选择一个成功的 workflow run，滚动到底部的 **Artifacts** 区域，下载你需要的压缩包（客户端选 `mmcprism` 变体，服务端选 `server` 变体）。

下载完成后，脚本会自动在本地查找并使用这些压缩包。

## 功能

- 自动在本地查找最新的构建压缩包（优先下载目录，其次脚本所在目录）
- 支持 `daily` 和 `experimental` 等构建类型，以及 `java8` / `new-java` 变体
- 兼容旧式“外层 artifact 内含 GTNH zip”和新版单层 zip 构建包
- 智能匹配新旧 mod，区分升级、降级、新增、用户自添加
- 合并 Forge `.cfg` 配置文件（保留已有设置值，仅添加新增项）
- （客户端 Prism/MultiMC）检测上级目录的 `libraries/`，更新新包中的 libraries，移除旧的 `lwjgl3ify`，并同步 pack JSON 中的 `lwjgl3ify` 版本
- （客户端）可选生成“差量资源 zip + 更新器 exe”玩家离线更新包
- 更新前自动备份所有 mod
- （服务端）自动更新 `server.properties` 中的 motd 构建类型和版本号
- 支持通过 `update_daily.cfg` 排除不需要新增或更新的 mod
- `--dry-run` 模式预览变更
- `--zip` 手动指定压缩包路径

## 使用方法

### 客户端

将 `update_dev_client.py` 放入 `.minecraft` 目录下：

```
.minecraft/
├── mods/
├── config/
└── update_dev_client.py  ← 放这里
```

```bash
# 自动查找最新的构建包并更新
python update_dev_client.py

# 仅预览，不做修改
python update_dev_client.py --dry-run

# 指定压缩包路径
python update_dev_client.py --zip path/to/gtnh-daily-xxx-mmcprism-new-java.zip
```

如果脚本位于 Prism/MultiMC 实例的 `.minecraft` 目录，且上级目录存在 `libraries/`、`mmc-pack.json` 或 `instance.cfg`，客户端更新时还会处理实例级 libraries：

```
instance/
├── libraries/
├── mmc-pack.json        # 或 Prism 实例名 .json
└── .minecraft/
    ├── mods/
    ├── config/
    └── update_dev_client.py
```

检测到该结构后，脚本会移除旧的 `lwjgl3ify` library 项，写入新包中的 `libraries/` 文件，并更新 `mmc-pack.json` 或实例根目录 `.json` 中的 `lwjgl3ify` 组件版本。相关文件会备份到 `.minecraft/back/prism_时间/`。

### 生成玩家离线更新包

该功能默认关闭。在客户端 `.minecraft/update_daily.cfg` 中开启：

```ini
[player_package]
enabled = true
```

使用原始 `mmcprism` 构建包执行客户端更新时，脚本会在 `.minecraft/player_updates/` 生成：

```
GTNH-player-package-<构建名>.zip
├── gtnh-player-update-<构建名>.zip  # 本次 mod/config/libraries 更新资源
└── update_dev_client.exe              # 玩家更新器
```

将这个外层 zip 发给玩家。玩家关闭游戏和启动器后，把其内两个文件解压到当前实例的 `.minecraft` 目录，再执行 `update_dev_client.exe`。exe 会自动选择同目录的差量包：

- 按 mod 名匹配玩家现有版本，更新时移除旧 jar，用户自行添加的 mod 保留不动。
- config 继续使用合并逻辑，保留玩家原有设置值。
- 如果检测到 Prism/MultiMC 实例结构，同时更新上级目录的 `libraries/` 和相关 pack JSON。
- 玩家电脑不需要安装 Python。

玩家包必须使用客户端更新脚本和 `mmcprism` 构建包生成；服务端构建包不包含客户端专用 mod 和 Prism libraries。
开启该功能时，脚本会先成功生成玩家包，再修改本地 mod/config/libraries；如果 exe 构建失败，本地更新会中止，以便修复环境后仍能重新生成完整差量。

### 服务端

将 `update_dev_server.py` 放入服务端根目录下：

```
server/
├── mods/
├── config/
└── update_dev_server.py  ← 放这里
```

```bash
# 自动查找最新的构建包并更新
python update_dev_server.py

# 仅预览，不做修改
python update_dev_server.py --dry-run

# 指定压缩包路径
python update_dev_server.py --zip path/to/gtnh-experimental-xxx-server-new-java.zip
```

## 排除配置

首次运行会自动生成 `update_daily.cfg` 配置文件。在 `[exclude]` 段下添加 mod 名称（不含版本号），可以排除不需要自动新增或更新的 mod：

```ini
[exclude]
# 每行一个 mod 名称（大小写不敏感）
SomeMod
AnotherMod

[player_package]
# 仅客户端脚本使用；默认 false
enabled = false
```

注意：排除会影响**新增**和**更新**。例如配置 `journeymap` 后，`journeymap-...-unlimited.jar → journeymap-...-fairplay.jar` 会被列入“配置排除”，本地已有的 jar 会保留不动。

旧版配置段 `[exclude_add]` 仍然兼容，行为与 `[exclude]` 相同。

## 压缩包搜索顺序

脚本会按以下顺序在本地搜索压缩包：

1. 用户下载目录（`~/Downloads`）
2. 脚本所在目录

| 脚本 | 匹配模式 | 示例 |
|------|----------|------|
| 客户端 | `gtnh-*-mmcprism-*.zip` | `gtnh-daily-2026-04-17+462-mmcprism-new-java.zip` |
| 服务端 | `gtnh-*-server-*.zip` | `gtnh-experimental-2026-04-17+105-server-new-java.zip` |

自动选择日期最新、构建号最大的版本。

## 环境要求

- Python 3.6+
- 无需额外依赖（仅使用标准库）
- 只有在 `.py` 模式下生成玩家 exe 时需要 PyInstaller：`python -m pip install pyinstaller`。如果当前工具本身已是 exe，会直接将自身放入玩家包。

## License

[GPL-3.0](LICENSE)
