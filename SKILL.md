---
name: claude-cleanup-ubuntu
description: 在 Ubuntu/Linux 上按上游 claude-cleanup 的范围审计、完整备份并清理 Claude Code 本机缓存和使用数据，支持可选本地身份轮换、CLI 凭据重置、桌面端数据清理、精简模式和时区设置。用于用户要求清理或重置 Ubuntu 上的 Claude 本机状态；保留会话、项目、skills、插件与未选择修改的配置。
---

# Claude 本机清理 · Ubuntu

使用随附的 `scripts/claude_cleanup.py`，以本 skill 目录为工作目录。要求 Linux 与 Python 3.8 或更新版本，仅使用 Python 标准库。保留上游的清理范围；不要临时增加删除目标或改成 `rm -rf`。

## 执行顺序

1. 先执行只读审计：

   ```bash
   python3 scripts/claude_cleanup.py --audit
   ```

2. 若当前执行者是 Claude Code，只完成审计，让使用者在普通终端或 Codex 中执行写操作。不要让正在使用配置目录的 Claude Code 自己清理该目录。

3. 根据审计结果说明实际配置目录、自动清理目标、完整备份位置，以及使用者选择的风险操作。用户已经要求清理时，不重复询问是否希望清理；由脚本展开最终清单并进行一次终端确认：

   ```bash
   python3 scripts/claude_cleanup.py
   ```

   不通过管道代答，不添加跳过确认的参数。原版的缓存与使用数据自动纳入最终清单，不逐项提问。身份轮换、凭据重置、桌面端数据、精简模式与时区仍为可选项。

4. 所有 Claude 进程已退出时执行自动清理。若 Claude 仍运行且未选择风险操作，跳过自动清理目标；若选择风险操作，让使用者正常退出后复检。不得自动 kill 进程。

5. 使用者输入 `CONFIRM` 后，先完整复制并校验配置目录及全局配置文件，再执行所选修改与隔离移动。保持备份和隔离批次，不自动永久删除。设置时区需要的 sudo 密码只由使用者在原生终端隐藏提示中输入，不读取、代填或记录。

6. 报告完整备份路径、隔离批次、实际移动清单、选中的重置项、遥测设置与时区结果。出现异常时明确说明已经完成哪些步骤，不将部分完成报告为全部成功。

## 自动清理范围

以下六项与上游一致，按实际存在情况自动纳入清单：

```text
<CLAUDE_CONFIG_DIR>/cache
<CLAUDE_CONFIG_DIR>/stats-cache.json
<CLAUDE_CONFIG_DIR>/telemetry
<CLAUDE_CONFIG_DIR>/usage-data
<CLAUDE_CONFIG_DIR>/usage.jsonl
<CLAUDE_CONFIG_DIR>/usage.with-fix.jsonl
```

将 Linux 的 `claude-cli-nodejs` 缓存和脚本列出的桌面端缓存/日志候选目录按白名单处理；准确路径以 `--audit` 和最终清单为准。`stats-cache.json`、`usage-data` 和 usage 文件可能包含历史统计，本版按用户指定的上游范围一并清理，不将它们描述为全部都能重新生成。

`CLAUDE_CONFIG_DIR` 未指定时使用 `~/.claude`。支持绝对路径的 XDG 缓存、配置和状态目录；不要凭空将所有 `~/.cache` 或 `~/.config` 都视为清理目标。

## 保护规则

保留配置目录中的以下内容，以及其他未列入白名单的路径：

```text
projects、sessions、history.jsonl、file-history、debug
skills、plugins、hooks、commands、scripts、agents、mcp-servers
backups、CLAUDE.md、settings.json、settings.local.json
plans、session-env、tasks、todos
```

保留所有项目目录、Git 仓库和 Codex 数据。拒绝通过符号链接或路径重叠绕过白名单。

只有明确选择相应操作时，才允许修改身份文件及其已知内部备份、修改 `settings.json` 中 `CLAUDE_CODE_SIMPLE`，或移动 `.credentials.json`。这些是上游可选重置行为在 Ubuntu 上的对应项，不扩大为清空整个配置目录。

## 可选项

- **本地身份轮换**：按上游逻辑更新 `userID`/`machineID`、移除已知账号缓存字段，并同步已知身份备份。保留其他键。
- **CLI 凭据重置**：将当前配置目录的 `.credentials.json` 移入隔离，替代 macOS 钥匙串操作。不得输出其中的 token；不修改 shell 中的 API key 或代理配置。
- **桌面端数据**：按脚本明确列出的 Linux/Electron 目录清理登录态与持久数据。不同 Linux 安装方式的程序卸载没有统一路径，不猜测包名或执行通配卸载。
- **精简模式**：只设置或移除 `settings.json` 的 `env.CLAUDE_CODE_SIMPLE`。不删除 skills、hooks、插件、MCP 或指令文件。
- **台北时区**：保留上游可选项，在系统支持 `timedatectl` 且当前不是 `Asia/Taipei` 时询问；不具备该能力的容器跳过此项。

逐值保留以下遥测键及其他未选择修改的设置：

```text
DISABLE_TELEMETRY
DISABLE_ERROR_REPORTING
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
```

## 安装与排障

将整个 `claude-cleanup-ubuntu` 目录放入目标 Ubuntu 上的 skill 目录，保留 `scripts/`，不能只复制本文件。也可以直接在普通终端运行随附脚本。

需要具体安装路径、Linux 候选目录、备份与隔离恢复说明或上游来源时，阅读 [Ubuntu 说明](references/ubuntu-notes.md)。运行回归测试时使用 `python3 -m unittest discover -s tests -v`，只使用临时测试目录。
