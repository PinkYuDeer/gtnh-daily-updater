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
- 智能匹配新旧 mod，区分升级、降级、新增、用户自添加
- 合并 Forge `.cfg` 配置文件（保留已有设置值，仅添加新增项）
- 更新前自动备份所有 mod
- （服务端）自动更新 `server.properties` 中的 motd 构建类型和版本号
- 支持通过 `update_daily.cfg` 排除不需要的新增 mod
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

首次运行会自动生成 `update_daily.cfg` 配置文件。在 `[exclude_add]` 段下添加 mod 名称（不含版本号），可以排除不需要自动新增的 mod：

```ini
[exclude_add]
# 每行一个 mod 名称（大小写不敏感）
SomeMod
AnotherMod
```

注意：排除仅影响**新增**的 mod，已有 mod 的版本更新不受影响。

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

## License

[GPL-3.0](LICENSE)
