# 率土战场 STZB Observer

前端、Python 后端、Electron 客户端、Windows 安装构建脚本，以及可恢复的历史业务数据库。

当前客户端版本：**0.2.4**。源码快照日期：**2026-11-15**。

## 目录

| 路径 | 内容 |
| --- | --- |
| `Dashboard/static/` | 地图、战报、阵容、考勤与战场网页 |
| `Dashboard/*.py` | 本机采集、档案、统计、查询与 HTTP 后端 |
| `Dashboard/desktop/` | Electron 客户端源码与界面测试 |
| `Dashboard/android_reader_assets/` | 采集组件源码、程序集及校验清单 |
| `Dashboard/catalog/` | 武将、战法、地形及定位资源 |
| `Dashboard/tests/` | 后端、前端与回放测试 |
| `Dashboard/windows-install/` | Windows 安装程序源码及构建脚本 |
| `SharedData/2026-11-15/` | 历史数据库压缩分卷及 SHA-256 清单 |
| `Tools/restore_shared_data.py` | 校验全部分卷并恢复 SQLite 数据库 |
| `Tools/export_shared_data.py` | 从 SQLite 一致读事务中导出可共享业务数据 |

## 恢复数据库

需要 Python 3.10 或更新版本。先下载整个仓库，确保分卷文件齐全，在仓库根目录执行：

```powershell
python Tools/restore_shared_data.py --database Dashboard/data/intelligence.sqlite3
```

恢复程序会检查每个分卷、压缩包、数据库的 SHA-256，验证 SQLite 完整性以及所有表的行数。
已有数据库或 WAL 文件时会拒绝覆盖；请先停止程序并备份原数据，或选择新的目标路径。

快照保留战报摘要及已采集全文、玩家与同盟资料、队伍阵容及历史、行军、考勤、阵容贡献、领地事件等业务表。
原始协议报文、客户端私有表事件、成员上传密钥和临时补采任务不发布；对应数据库表保留结构、内容为空。
游戏内名称、角色和同盟标识保留，以维持历史记录之间的关联。这是一份历史资料，不包含游戏登录权限。
快照时间、各表行数和档案范围以 `SharedData/2026-11-15/manifest.json` 为准。

## 运行和构建

程序目前用于 **Windows 本机**，HTTP 服务默认只监听 `127.0.0.1`。本次上传未进行云端多人服务改造。
登录角色、区服、赛季绑定仍由现有程序核对；恢复数据库不会自动把其他角色的历史数据合并到当前角色。
本机的 `config.json`、`capture-source.json`、`archive-profile.json`、运行日志与抓包文件不随源码发布。

桌面发行版使用 Python、Pillow、tzdata、Electron、Frida 和 Wireshark。
构建入口为 `Dashboard/windows-install/build_desktop_installer.py`；运行 `--help` 查看需要提供的依赖目录。
第三方运行库、抓包驱动及安装包需按构建脚本的版本和 SHA-256 单独准备；仓库提供构建源码，未包含预编译安装 EXE。
内部抓包诊断与程序集分析工具继续留在本地，采集所需的帧解析器由 `Dashboard/capture_decoder.py` 提供。

## 检查

```powershell
$env:PYTHONPATH="Dashboard;Dashboard/tests"
python -m unittest discover -s Dashboard/tests -p "test_*.py"
Get-ChildItem Dashboard/tests/test_*.js | ForEach-Object { node $_.FullName }
Get-ChildItem Dashboard/desktop/test_*.js | ForEach-Object { node $_.FullName }
```

部分集成测试需要本机监听端口或 Windows 系统接口；在限制这些能力的环境中会失败。
