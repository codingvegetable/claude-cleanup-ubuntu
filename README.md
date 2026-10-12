# Claude Cleanup Ubuntu

面向 **Ubuntu/Linux、Python 3.8+** 的 Claude Code 本机状态清理 skill，仅使用 Python 标准库。按上游 `claude-cleanup` 的范围审计、完整备份并隔离缓存和使用数据，支持使用者选择本地重置项。

这是基于 [suyuan2022/suyuan-skill/claude-cleanup](https://github.com/suyuan2022/suyuan-skill/tree/main/claude-cleanup) 的 Ubuntu 适配，采用 MIT 许可证。

## 获取与运行

需要安装 Git 和 Python 3.8 或更新版本。这是公开仓库，使用下面的 HTTPS 地址克隆无需登录 GitHub。

```bash
git clone https://github.com/codingvegetable/claude-cleanup-ubuntu.git
cd claude-cleanup-ubuntu
```

先执行**只读审计**，查看实际路径与清理候选项：

```bash
python3 scripts/claude_cleanup.py --audit
```

退出相关 Claude 进程后，在普通交互式终端运行：

```bash
python3 scripts/claude_cleanup.py
```

脚本询问可选操作、展示最终清单，输入 `CONFIRM` 后才备份和执行。取消时不创建备份或隔离目录。不要通过 `sudo python3` 运行整份脚本；可选时区步骤会单独处理所需权限。

## 作为 skill 安装

将完整仓库目录放到 `~/.claude/skills/claude-cleanup-ubuntu/`，保留 `scripts/` 等配套文件。也可以让 Codex 读取仓库中的 [SKILL.md](SKILL.md) 并按流程执行。

在 Claude Code 内仅执行 `--audit`；实际清理应在普通终端或 Codex 中完成，避免正在使用配置的 Claude Code 清理自身状态。

## 清理范围

默认配置目录是 `~/.claude`，支持通过 `CLAUDE_CONFIG_DIR` 指定配置目录。以下六项存在时自动纳入最终清单，与上游范围一致：

```text
cache
stats-cache.json
telemetry
usage-data
usage.jsonl
usage.with-fix.jsonl
```

Linux 的 `claude-cli-nodejs` 缓存和脚本明确列出的桌面端缓存也按白名单处理，支持 XDG 目录变量。统计和 usage 文件可能包含历史使用数据；准确目录以审计结果和最终清单为准。

| 可选操作 | 行为 |
| --- | --- |
| 本地身份轮换 | 更新 `userID`/`machineID`，移除已知账号缓存字段并同步已知身份备份 |
| CLI 凭据重置 | 将当前配置目录中的 `.credentials.json` 移入隔离 |
| 桌面端数据清理 | 处理明确列出的 Linux/Electron 持久数据目录 |
| 精简模式 | 设置或移除 `settings.json` 中的 `env.CLAUDE_CODE_SIMPLE` |
| 时区设置 | 系统支持时，可选择通过 `timedatectl` 设置为 `Asia/Taipei` |

会话、项目、历史记录、skills、插件、hooks、MCP、debug 和未选择修改的配置保留；遥测设置逐值保留。脚本拒绝通过符号链接或路径重叠绕过白名单，不自动卸载桌面应用。

## 备份与恢复

| 内容 | 默认位置 |
| --- | --- |
| 完整备份 | `~/ClaudeBackups/claude-cleanup-<时间戳>/` |
| 隔离批次 | `~/.local/state/claude-cleanup-ubuntu/trash/<批次>/`；设置 `XDG_STATE_HOME` 时使用对应状态目录 |
| 移动记录 | 隔离批次中的 `metadata.json` |

脚本先完整复制并校验配置目录及全局配置文件，校验成功后才执行修改与隔离移动。备份和隔离副本均保留，不自动永久删除，因此清理后不会立即释放这些副本占用的磁盘空间。

恢复时先退出 Claude，再根据移动记录将所需文件恢复到原位置；原位置已有新数据时保留新旧两份。具体路径、保护规则与恢复方法见 [Ubuntu 安装与行为说明](references/ubuntu-notes.md)。

## 测试与验证范围

随附 **19 项隔离测试**，覆盖备份校验、自动清理、可选重置、配置保护、取消、只读审计及符号链接边界：

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时目录并替换真实进程和时区操作。本适配尚未在使用者的 Ubuntu 主机上实测；实际安装方式与路径请通过 `--audit` 核对。

## 来源与许可证

上游项目：[suyuan2022/suyuan-skill](https://github.com/suyuan2022/suyuan-skill/tree/main/claude-cleanup)。完整来源版本信息见 [Ubuntu 说明](references/ubuntu-notes.md#来源与许可证)。

保留上游 `Copyright (c) 2026 Suyuan` 与完整 [MIT 许可证](LICENSE)。本项目是独立的 Ubuntu 适配，上游作者未将其作为官方 Linux 版本发布。
