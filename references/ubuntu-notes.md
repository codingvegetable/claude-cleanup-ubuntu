# Ubuntu 安装与行为说明

## 使用方式

将完整的 `claude-cleanup-ubuntu` 目录放到 Ubuntu 上；不要仅复制 `SKILL.md`。脚本仅依赖 Python 3.8+ 标准库，不需要安装额外 Python 包，也不需要安装 Claude Desktop。

普通终端使用：

```bash
cd /实际位置/claude-cleanup-ubuntu
python3 scripts/claude_cleanup.py --audit
python3 scripts/claude_cleanup.py
```

第一条是只读审计。第二条依次询问原版可选重置项，展开清单，输入 `CONFIRM` 后执行。取消时不创建备份或隔离目录。不要通过 `sudo python3` 运行整份脚本，否则会变为处理 root 用户的目录；只有可选时区步骤使用 sudo。

作为 Claude Code skill 使用时，把目录放在 `~/.claude/skills/claude-cleanup-ubuntu/`。Claude Code 内只做 `--audit`，实际写操作交给普通终端或 Codex。也可让 Codex 读取完整目录中的 `SKILL.md` 后按其中流程执行。

## 路径与移植范围

| 项目 | Ubuntu 版处理 |
| --- | --- |
| Claude Code 根目录 | `CLAUDE_CONFIG_DIR`，未设置时 `~/.claude` |
| 全局身份文件 | 默认 `~/.claude.json`；自定义配置目录内已有 `.claude.json` 时优先使用该文件，最终清单显示实际位置 |
| CLI 缓存和统计 | 原版六项：`cache`、`stats-cache.json`、`telemetry`、`usage-data`、`usage.jsonl`、`usage.with-fix.jsonl` |
| CLI 包缓存 | `${XDG_CACHE_HOME:-~/.cache}/claude-cli-nodejs` |
| 桌面端缓存 | XDG 缓存根目录下的 `Claude`、`Claude-3p`、`claude-desktop`；与原版一样自动纳入白名单 |
| 桌面端持久数据 | XDG 配置根目录下的 `Claude`、`Claude-3p`、`claude-desktop`；选择桌面数据清理后才处理 |
| CLI 保存的登录凭据 | 当前配置目录内 `.credentials.json`，选择重置后移入隔离 |
| 完整备份 | `~/ClaudeBackups/claude-cleanup-<时间戳>/` |
| 隔离批次 | `${XDG_STATE_HOME:-~/.local/state}/claude-cleanup-ubuntu/trash/<批次>/` |
| 精简模式 | 原版 `env.CLAUDE_CODE_SIMPLE` 设置/移除逻辑 |
| 时区 | 系统支持时使用 `timedatectl`，原版目标 `Asia/Taipei` 保留为可选项 |

XDG 路径由本移植版按 Linux 目录约定解析，不是对所有 Claude 安装版本目录布局的保证。变量缺省时使用上表默认值；相对 XDG 路径会报错并停止，需取消该变量或改成绝对路径。不要把桌面端候选表扩展成清空整个配置或缓存根目录。

Linux 的桌面端可能由不同安装器提供。此版保留桌面数据清理，不自动猜测 apt、snap、Flatpak 包名或移动不确定的程序目录。程序卸载需要按实际安装方式另行处理。

## 备份与恢复

完整备份保留配置树、全局配置文件和校验清单；普通文件用 SHA256 校验，符号链接按链接保留，不递归复制外部链接目标。备份校验失败时，不执行后续清理。

所有清理目标移入带时间戳的隔离批次；`metadata.json` 的 `entries` 记录 `original`、`quarantine` 和移动状态。恢复时先退出 Claude，查看对应批次的移动记录，再将需要的项放回原始位置。若原位置已经有新数据，先比较并保留新旧两份，不覆盖。身份轮换或精简模式需要回退时，从完整备份中选择对应文件；不要直接用旧的整个配置树覆盖后来新建的会话。

备份和隔离数据保留在磁盘上，因此本版清理的是 Claude 正在使用的本机状态，**不会自动立即释放这些文件占用的全部磁盘空间**。它也不会自动清空隔离目录或备份目录。

## 与原版相同的数据边界

本版按使用者要求保留原版清理程度。统计和 usage 文件也会进入最终清单，其中的历史统计不会继续留在原路径；这不是只清理 changelog 的收窄版本。会话、项目、skills、plugins、hooks、MCP、debug 和其他保护项仍保留。遥测设置逐值继承，不因清理遥测缓存而主动更改遥测开关。

本地 ID 轮换仅修改原项目已定义的本地字段，不代表能改变服务端的账号或设备判断。

## 回归验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时目录，mock 真实进程和时区操作，不需要连接服务端、登录 Claude、输入 sudo 密码或修改实际配置。实际发行版/桌面安装器差异以目标机器上的 `--audit` 为准。

## 来源与许可证

- 上游：[suyuan2022/suyuan-skill/claude-cleanup](https://github.com/suyuan2022/suyuan-skill/tree/main/claude-cleanup)。
- 参考脚本 blob SHA：`248c2025dda27eebd45d5925e3a32aa6cfd6478e`，参考主分支 commit：`423473db6b618c7e040f1a5a6879bc83e69e8b3b`。
- 保留上游 `Copyright (c) 2026 Suyuan` 及完整 MIT 许可证，见本目录 `LICENSE`。
- 本目录是基于上述源码的 Ubuntu 适配，不是上游作者正式发布的 Linux 版本。
- 目录变量依据：[XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/)。
- 配置变量参考：[Claude Code environment variables](https://code.claude.com/docs/en/env-vars)。
