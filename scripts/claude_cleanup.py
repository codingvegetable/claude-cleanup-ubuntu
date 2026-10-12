#!/usr/bin/env python3
"""在 Ubuntu / Linux 上审计并隔离清理 Claude Code 的本机状态。

Adapted from suyuan2022/suyuan-skill/claude-cleanup (MIT).
Python 3.8+; standard library only. No-argument mode requires an interactive TTY.
This script never removes the quarantine or the verified backup.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Set, Tuple


TELEMETRY_KEYS = ("DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")
ACCOUNT_CACHE_KEYS = {
    "additionalModelCostsCache", "additionalModelOptionsCache", "anonymousId", "autoCompactWindowsCache",
    "cachedChromeExtensionInstalled", "cachedDynamicConfigs", "cachedExperimentData", "cachedExperimentFeatures",
    "cachedExtraUsageDisabledReason", "cachedGrowthBookFeatures", "cachedGrowthBookFeaturesAt", "cachedStatsigGates",
    "clientDataCache", "clientDataCacheSlots", "feedbackSurveyState", "groveConfigCache", "metricsStatusCache",
    "modelAccessCache", "oauthAccount", "orgModelDefaultCache", "passesEligibilityCache", "s1mAccessCache",
}
PROTECTED = {
    "CLAUDE.md", "agents", "backups", "commands", "debug", "file-history", "history.jsonl", "hooks",
    "mcp-servers", "plans", "plugins", "projects", "scripts", "session-env", "sessions", "settings.json",
    "settings.local.json", "skills", "tasks", "todos",
}
# Keep the upstream allowlist, including its usage/statistics files.
CLI_CACHE = ("cache", "stats-cache.json", "telemetry", "usage-data", "usage.jsonl", "usage.with-fix.jsonl")
DESKTOP_NAMES = ("Claude", "Claude-3p", "claude-desktop")
# Linux directories are constructed through XDG below; no broad name globs.
SAFE_RELATIVE = (".cache/claude-cli-nodejs",) + tuple(".cache/" + name for name in DESKTOP_NAMES)
SAFE_GLOBS = ()
DESKTOP_RELATIVE = tuple(".config/" + name for name in DESKTOP_NAMES)
DESKTOP_GLOBS = ()


def _absolute(path: pathlib.Path) -> pathlib.Path:
    """Normalize lexically, without resolving a link or accepting '..'."""
    path = pathlib.Path(path)
    if ".." in path.parts:
        raise RuntimeError("路径不得包含 '..'：{}".format(path))
    return pathlib.Path(os.path.abspath(str(path)))


def _inside(path: pathlib.Path, directory: pathlib.Path) -> bool:
    path, directory = _absolute(path), _absolute(directory)
    return path == directory or directory in path.parents


def _assert_no_symlink(path: pathlib.Path) -> None:
    """Refuse a linked target or ancestor before reading/writing a managed path.

    Links *inside* a backed-up/moved directory are copied/moved as links. They
    are not followed into other projects or files outside that directory.
    """
    path = _absolute(path)
    for component in list(reversed(path.parents)) + [path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("拒绝通过符号链接操作目标或其父目录：{}".format(component))


def _exists(path: pathlib.Path) -> bool:
    return path.exists() or path.is_symlink()


def xdg_path(home: pathlib.Path, variable: str, default: str) -> pathlib.Path:
    value = os.environ.get(variable, "")
    if not value:
        return _absolute(home / default)
    path = pathlib.Path(value)
    if not path.is_absolute():
        raise ValueError("{} 必须是绝对路径；请取消该变量或改为绝对路径".format(variable))
    return _absolute(path)


def claude_config_dir(home: pathlib.Path) -> pathlib.Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if not value:
        return _absolute(home / ".claude")
    # Also accept a literal leading ~, interpreted against the selected home.
    if value == "~":
        path = home
    elif value.startswith("~/"):
        path = home / value[2:]
    else:
        path = pathlib.Path(value)
    if not path.is_absolute():
        raise ValueError("CLAUDE_CONFIG_DIR 必须是绝对路径（或以 ~/ 开头）")
    return _absolute(path)


def identity_path(home: pathlib.Path) -> pathlib.Path:
    claude = claude_config_dir(home)
    custom_identity = claude / ".claude.json"
    # Some installations put the global file under a custom config directory.
    # Do not modify two unrelated identities just because both files exist.
    if os.environ.get("CLAUDE_CONFIG_DIR") and _exists(custom_identity):
        return custom_identity
    return _absolute(home / ".claude.json")


def credential_path(home: pathlib.Path) -> pathlib.Path:
    return claude_config_dir(home) / ".credentials.json"


def quarantine_root(home: pathlib.Path) -> pathlib.Path:
    return xdg_path(home, "XDG_STATE_HOME", ".local/state") / "claude-cleanup-ubuntu" / "trash"


def _private_directory(path: pathlib.Path) -> None:
    """Create only missing ancestors with 0700; protect this directory too."""
    path = _absolute(path)
    _assert_no_symlink(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    _assert_no_symlink(path)
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    os.chmod(path, 0o700)


def read_json(path: pathlib.Path, default: Optional[object] = None) -> object:
    _assert_no_symlink(path)
    if not path.exists() and default is not None:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, data: object) -> None:
    _assert_no_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink(path)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".{}.".format(path.name), dir=str(path.parent))
    temp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        _assert_no_symlink(path)
        os.replace(str(temp), str(path))
    finally:
        temp.unlink(missing_ok=True)


def settings_env(data: object) -> Dict[str, object]:
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise ValueError("settings.json 的顶层或 env 结构异常，拒绝修改")
    return data.get("env", {})


def telemetry(data: object) -> Dict[str, Optional[object]]:
    env = settings_env(data)
    return {key: env.get(key) for key in TELEMETRY_KEYS}


def _protected_references(claude: pathlib.Path) -> Set[pathlib.Path]:
    """Find protected symlink references without traversing their targets.

    For example, projects -> cache/live-data and skills/demo -> cache/assets
    make cache ineligible for cleanup. Only actual protected directories are
    walked; resolve() inspects link/path metadata, not external file contents.
    """
    _assert_no_symlink(claude)
    references = set()
    pending = [claude / name for name in PROTECTED]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            try:
                references.add(path.resolve(strict=False))
            except (OSError, RuntimeError) as error:
                raise RuntimeError("无法确定受保护符号链接的引用路径：{}".format(path)) from error
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(str(path)) as iterator:
                pending.extend(pathlib.Path(entry.path) for entry in iterator)
    return references


def _validate_target(target: pathlib.Path, allowed: Set[pathlib.Path], claude: pathlib.Path) -> None:
    absolute, claude = _absolute(target), _absolute(claude)
    protected = {claude / name for name in PROTECTED}
    if _inside(claude, absolute) or any(_inside(absolute, path) for path in protected):
        raise RuntimeError("拒绝触碰受保护路径：{}".format(target))
    if absolute not in {_absolute(path) for path in allowed}:
        raise RuntimeError("目标不在清理白名单：{}".format(target))
    _assert_no_symlink(absolute)
    for reference in _protected_references(claude):
        if _inside(reference, absolute) or _inside(absolute, reference):
            raise RuntimeError("清理目标与受保护符号链接的实际引用路径重叠，拒绝移动：{}".format(target))


def discover(home: pathlib.Path) -> Tuple[List[pathlib.Path], List[pathlib.Path]]:
    claude = claude_config_dir(home)
    cache = xdg_path(home, "XDG_CACHE_HOME", ".cache")
    config = xdg_path(home, "XDG_CONFIG_HOME", ".config")
    # Validate XDG_STATE_HOME even during --audit, without creating it.
    quarantine_root(home)
    safe = [claude / name for name in CLI_CACHE] + [cache / "claude-cli-nodejs"]
    safe += [cache / name for name in DESKTOP_NAMES]
    desktop = [config / name for name in DESKTOP_NAMES]
    safe = sorted({path for path in safe if _exists(path)}, key=str)
    desktop = sorted({path for path in desktop if _exists(path)}, key=str)
    # Audit also reports unsafe topology instead of silently following a link.
    for path in safe + desktop:
        _validate_target(path, set(safe + desktop), claude)
    return safe, desktop


def _is_claude_process(command: str, args: str) -> bool:
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    names = {"claude", "claude-code", "claude-desktop", "claude-3p"}
    first = pathlib.Path(tokens[0]).name.lower() if tokens else ""
    if pathlib.Path(command).name.lower() in names or first in names:
        return True
    # npm-based Claude Code runs with 'node' as its process name.
    if pathlib.Path(command).name.lower() in {"node", "nodejs", "bun"} or first in {"node", "nodejs", "bun"}:
        return any("/@anthropic-ai/claude-code/" in token and pathlib.Path(token).name == "cli.js" for token in tokens)
    return False


def claude_processes() -> List[str]:
    result = subprocess.run(["ps", "-axo", "pid=,comm=,args="], capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0:
        raise RuntimeError("无法读取进程列表；请确认 procps/ps 可用")
    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 2 or parts[0] == str(os.getpid()):
            continue
        if _is_claude_process(parts[1], parts[2] if len(parts) == 3 else ""):
            found.append(line.strip())
    return found


def running_under_claude() -> bool:
    pid = os.getppid()
    for _ in range(24):
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=,comm=,args="],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError("无法检查父进程；请确认 procps/ps 可用")
        parts = result.stdout.strip().split(maxsplit=2)
        if len(parts) < 2:
            return False
        if _is_claude_process(parts[1], parts[2] if len(parts) == 3 else ""):
            return True
        next_pid = int(parts[0])
        if next_pid <= 1 or next_pid == pid:
            return False
        pid = next_pid
    raise RuntimeError("父进程链过长，无法确认执行环境；请从普通终端运行")


def _timedatectl_timezone() -> Optional[str]:
    executable = shutil.which("timedatectl")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "show", "--property=Timezone", "--value"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def timezone_supported() -> bool:
    if os.geteuid() != 0 and not shutil.which("sudo"):
        return False
    return _timedatectl_timezone() is not None


def current_timezone() -> str:
    detected = _timedatectl_timezone()
    if detected:
        return detected
    try:
        target = os.readlink("/etc/localtime")
        if "/zoneinfo/" in target:
            return target.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        return pathlib.Path("/etc/timezone").read_text(encoding="utf-8").strip() or "未知"
    except OSError:
        return "未知"


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_manifest(root: pathlib.Path) -> Dict[str, Dict[str, object]]:
    """Hash regular files; record directories/links without following links."""
    result = {}

    def visit(directory: pathlib.Path) -> None:
        with os.scandir(str(directory)) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            path = pathlib.Path(entry.path)
            relative = path.relative_to(root).as_posix()
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                result[relative] = {"type": "symlink", "target": os.readlink(str(path))}
            elif stat.S_ISDIR(info.st_mode):
                result[relative] = {"type": "directory"}
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                result[relative] = {"type": "file", "size": info.st_size, "sha256": _sha256(path)}
            else:
                raise RuntimeError("完整备份不支持套接字/设备/FIFO 等特殊文件：{}".format(path))

    visit(root)
    return result


def create_backup(home: pathlib.Path) -> pathlib.Path:
    claude, identity = claude_config_dir(home), identity_path(home)
    _assert_no_symlink(claude)
    if not claude.is_dir():
        raise FileNotFoundError("Claude 配置目录不存在，无法满足先全量备份的硬要求：{}".format(claude))
    backup_root = _absolute(home / "ClaudeBackups")
    if _inside(backup_root, claude):
        raise RuntimeError("备份目录不得位于 Claude 配置目录内：{}".format(backup_root))
    _assert_no_symlink(backup_root)
    identities = []
    for candidate in (identity, _absolute(home / ".claude.json")):
        if candidate not in identities and _exists(candidate):
            _assert_no_symlink(candidate)
            if not candidate.is_file():
                raise ValueError("身份文件不是普通文件：{}".format(candidate))
            identities.append(candidate)
    before = _tree_manifest(claude)
    identities_before = {str(path): _sha256(path) for path in identities}
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    partial = backup_root / "claude-cleanup-{}.partial".format(stamp)
    final = partial.with_suffix("")
    # No writes occur above this line. This is the first write in a cleanup run.
    _private_directory(backup_root)
    partial.mkdir(mode=0o700)
    shutil.copytree(str(claude), str(partial / "dot-claude"), symlinks=True)
    copied = _tree_manifest(partial / "dot-claude")
    after = _tree_manifest(claude)
    if before != copied or before != after:
        raise RuntimeError("Claude 配置备份 SHA256/目录/链接校验失败或源目录变化，保留未完成副本：{}".format(partial))
    identity_entries = []
    for path in identities:
        filename = "claude.json" if path == identity else "home-claude.json"
        destination = partial / filename
        shutil.copy2(str(path), str(destination))
        expected = identities_before[str(path)]
        if expected != _sha256(destination) or expected != _sha256(path):
            raise RuntimeError("身份文件备份 SHA256 校验失败或源文件变化，保留未完成副本：{}".format(partial))
        identity_entries.append({"source": str(path), "backup": filename, "sha256": expected})
    regular = [entry for entry in before.values() if entry["type"] == "file"]
    write_json(partial / "manifest.json", {
        "files": len(regular),
        "regularFileBytes": sum(entry["size"] for entry in regular),
        "claudeJsonCopied": identity in identities,
        "configDirectory": str(claude),
        "identityFile": str(identity),
        "identityFiles": identity_entries,
        "verification": "sha256+entry-types+symlink-targets",
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tree": before,
    })
    partial.rename(final)
    return final


def move_to_trash(target: pathlib.Path, allowed: Set[pathlib.Path], claude: pathlib.Path, batch: pathlib.Path) -> pathlib.Path:
    target, batch = _absolute(target), _absolute(batch)
    _validate_target(target, allowed, claude)
    if _inside(batch, target) or _inside(target, batch):
        raise RuntimeError("隔离目录不得与清理目标相互包含")
    _assert_no_symlink(batch)
    if not _exists(target):
        raise FileNotFoundError("待隔离目标已不存在：{}".format(target))
    _private_directory(batch.parent)
    _private_directory(batch)
    manifest_path = batch / "metadata.json"
    metadata = read_json(manifest_path, {"version": 1, "entries": []})
    if not isinstance(metadata, dict) or not isinstance(metadata.get("entries"), list):
        raise RuntimeError("隔离批次 metadata.json 结构异常")
    sequence = len(metadata["entries"]) + 1
    destination = batch / "{:04d}-{}".format(sequence, target.name)
    if _exists(destination):
        raise RuntimeError("隔离目标已存在，拒绝覆盖：{}".format(destination))
    entry = {
        "original": str(target), "quarantine": str(destination),
        "state": "planned", "moved": False,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    metadata["entries"].append(entry)
    # Write the recovery mapping BEFORE moving, including for cross-device moves.
    write_json(manifest_path, metadata)
    _validate_target(target, allowed, claude)
    shutil.move(str(target), str(destination))
    entry.update({"state": "moved", "moved": True})
    write_json(manifest_path, metadata)
    return destination


def rotate_identity(home: pathlib.Path) -> int:
    identity, backups = identity_path(home), claude_config_dir(home) / "backups"
    _assert_no_symlink(identity)
    if not identity.is_file():
        raise FileNotFoundError("身份文件不存在，无法轮换本地 ID：{}".format(identity))
    files = [identity]
    _assert_no_symlink(backups)
    if backups.is_dir():
        files += sorted(path for path in backups.glob(".claude.json.backup.*") if path.is_file() and not path.is_symlink())
    ids, prepared = {"userID": secrets.token_hex(32), "machineID": secrets.token_hex(32)}, []
    for path in files:
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError("身份文件不是 JSON object：{}".format(path))
        updated = dict(data)
        updated.update(ids)
        for key in ACCOUNT_CACHE_KEYS:
            updated.pop(key, None)
        prepared.append((path, updated))
    for path, data in prepared:
        write_json(path, data)
    return len(files) - 1


def update_simple(home: pathlib.Path, mode: int) -> None:
    if mode == 0:
        return
    if mode not in {1, 2}:
        raise ValueError("精简模式必须是 0、1 或 2")
    path = claude_config_dir(home) / "settings.json"
    if mode == 2 and not _exists(path):
        return
    data = read_json(path, {})
    if not isinstance(data, dict):
        raise ValueError("settings.json 顶层必须是 JSON object")
    before, updated, env = telemetry(data), dict(data), dict(settings_env(data))
    if mode == 1:
        env["CLAUDE_CODE_SIMPLE"] = "1"
    else:
        env.pop("CLAUDE_CODE_SIMPLE", None)
    updated["env"] = env
    if telemetry(updated) != before:
        raise RuntimeError("精简模式修改触碰了遥测设置")
    if updated != data:
        write_json(path, updated)


def ask_yes(prompt: str) -> bool:
    return input("{} [y/N]：".format(prompt)).strip().lower() == "y"


def ask_choice(prompt: str, allowed: Set[int]) -> int:
    value = input(prompt).strip() or "0"
    if not value.isdigit() or int(value) not in allowed:
        raise ValueError("输入不在允许范围内，已停止且未修改")
    return int(value)


def _validate_storage(home: pathlib.Path, claude: pathlib.Path, targets: List[pathlib.Path]) -> None:
    backup_root = _absolute(home / "ClaudeBackups")
    trash_root = quarantine_root(home)
    for root in (backup_root, trash_root):
        _assert_no_symlink(root)
        if _inside(root, claude):
            raise RuntimeError("备份/隔离目录不得位于 Claude 配置目录内：{}".format(root))
        for target in targets:
            if _inside(root, target) or _inside(target, root):
                raise RuntimeError("清理目标与备份/隔离目录重叠：{}".format(target))
    if _inside(trash_root, backup_root) or _inside(backup_root, trash_root):
        raise RuntimeError("备份与隔离目录不得相互包含")
    for target in targets:
        _validate_target(target, set(targets), claude)
        for other in targets:
            if target != other and _inside(target, other):
                raise RuntimeError("清理目标嵌套，拒绝重复移动：{}".format(target))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="在 Ubuntu / Linux 上审计和隔离清理 Claude 本机状态（Python 3.8+）")
    parser.add_argument("--audit", action="store_true", help="只读审计；不写配置、备份或隔离目录")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        print("拒绝执行：此移植版只支持 Ubuntu / Linux。", file=sys.stderr)
        return 2
    backup = None
    batch = None
    try:
        home = _absolute(pathlib.Path.home())
        claude, identity = claude_config_dir(home), identity_path(home)
        settings, credentials = claude / "settings.json", credential_path(home)
        if not args.audit and running_under_claude():
            print("拒绝执行：当前脚本由 Claude Code 启动。请改用 Codex 或普通终端；这里只允许 --audit。", file=sys.stderr)
            return 2
        settings_data = read_json(settings, {})
        env = settings_env(settings_data)
        safe, desktop = discover(home)
        processes = claude_processes()
        configured_telemetry = [key for key in TELEMETRY_KEYS if key in env]
        print("Claude Cleanup Ubuntu 按上游白名单自动清理缓存、日志及本地使用统计；其他操作由你选择。")
        print("使用统计不保证能重新生成；完整备份与隔离副本用于恢复。")
        print("输入最终 CONFIRM 前不会写盘；所有移除只进入隔离目录。\n\n只读审计：")
        print("- 配置目录：{}（{}）".format(claude, "存在" if claude.is_dir() else "不存在"))
        print("- 本次身份文件：{}（{}）".format(identity, "存在" if identity.is_file() else "不存在"))
        if identity != home / ".claude.json" and _exists(home / ".claude.json"):
            print("- 另行备份默认身份文件 {}，本次身份轮换不修改它。".format(home / ".claude.json"))
        print("- 自动清理项：{}；可选桌面持久数据：{}；相关进程：{} 个".format(len(safe), len(desktop), len(processes)))
        print("- Linux CLI 凭据文件：{}（{}；不会显示文件内容）".format(credentials, "存在" if _exists(credentials) else "未检测到"))
        print("- 已配置遥测键：{}（配置值逐值保持不变）".format(", ".join(configured_telemetry) if configured_telemetry else "未检测到"))
        print("- 精简模式：{}；时区：{}".format("已启用" if str(env.get("CLAUDE_CODE_SIMPLE", "")).lower() in {"1", "true"} else "未启用", current_timezone()))
        print("- 桌面应用安装器不统一；本脚本不支持自动卸载 apt/snap/flatpak/AppImage 应用。")
        for label, candidates in (("自动清理候选", safe), ("可选桌面数据", desktop)):
            for path in candidates:
                print("  {}：{}".format(label, path))
        if args.audit:
            return 0
        if not sys.stdin.isatty():
            print("拒绝执行：需要真实 TTY 完成知情确认。", file=sys.stderr)
            return 2

        print("\n仅询问上游的可选操作；自动清理白名单不逐项询问。")
        rotate = ask_yes("轮换本地 userID/machineID，并清除已知账号缓存字段？")
        remove_credentials = ask_yes("将 Linux 的 .credentials.json 移入隔离（可能使 CLI 需要重新登录）？")
        desktop_mode = ask_choice("Claude Desktop：0 保留；1 清登录态/持久数据；2 同 1 并说明如何处理应用（不自动卸载）。选择 [0]：", {0, 1, 2})
        simple_mode = ask_choice("精简模式：0 保持；1 启用 CLAUDE_CODE_SIMPLE；2 移除该设置。选择 [0]：", {0, 1, 2})
        set_timezone = False
        if timezone_supported():
            set_timezone = current_timezone() != "Asia/Taipei" and ask_yes("把 Ubuntu 系统时区改为 Asia/Taipei（影响整机）？")
        else:
            print("当前环境无可用的 systemd/timedatectl 或所需 sudo；不支持自动修改时区，已跳过时区询问。")
        risky = rotate or remove_credentials or desktop_mode or simple_mode
        clean_safe = not processes
        if processes and risky:
            print("\n所选操作要求你正常退出所有 Claude Code / Claude Desktop；脚本不会 kill 进程。")
            input("退出后按 Enter 重新检查：")
            if claude_processes():
                print("仍检测到 Claude 进程，已取消且未写盘。", file=sys.stderr)
                return 2
            clean_safe = True
        targets = (safe if clean_safe else []) + (desktop if desktop_mode else [])
        if remove_credentials and _exists(credentials):
            targets.append(credentials)
        targets = list(dict.fromkeys(targets))
        _validate_storage(home, claude, targets)

        print("\n最终执行清单：")
        print("1. 第一个写操作：完整备份并校验 {}，以及现存身份文件。".format(claude))
        print("   备份根目录：{}；按 SHA256、条目类型和链接目标核验。".format(home / "ClaudeBackups"))
        action = "自动隔离清理" if clean_safe else "因 Claude 正在运行而跳过"
        print("2. {} {} 项缓存/日志/本地使用统计。".format(action, len(safe)))
        for target in targets:
            print("   - {}".format(target))
        print("3. 身份轮换：{}；清 CLI 凭据：{}；精简模式：{}；修改时区：{}".format("是" if rotate else "否", "是" if remove_credentials else "否", simple_mode, "是" if set_timezone else "否"))
        print("   轮换所用身份文件：{}；隔离根目录：{}".format(identity, quarantine_root(home)))
        print("受保护目录不参与清理：sessions、projects、history、debug、skills、plugins、hooks、commands、agents、MCP 等。")
        print("内部身份备份仅在选择轮换时同步；settings.json 仅按精简模式选择修改；遥测键逐值保持。")
        if desktop_mode == 2:
            print("应用移除：不支持自动卸载。清理完成后请使用实际安装器或原安装说明处理应用。")
        if input("完全理解后输入 CONFIRM 开始；其他输入取消：").strip() != "CONFIRM":
            print("已取消，没有修改任何内容。")
            return 1

        # If Claude started while the user was deciding, do not mutate live data.
        now_running = claude_processes()
        if now_running and risky:
            print("确认后检测到 Claude 进程，已取消且未写盘；请退出后重新运行。", file=sys.stderr)
            return 2
        if now_running and clean_safe:
            clean_safe = False
            targets = [path for path in targets if path not in safe]
            print("确认期间出现 Claude 进程；本次跳过自动缓存/统计清理。")
        before_telemetry = telemetry(read_json(settings, {}))
        protected_before = [claude / name for name in PROTECTED if _exists(claude / name)]
        _validate_storage(home, claude, targets)
        backup = create_backup(home)  # First write; refuses unverified copies.
        print("\n[1/6] 全量备份已校验：{}".format(backup))
        timezone_command = []
        if set_timezone:
            executable = shutil.which("timedatectl")
            if not executable:
                raise RuntimeError("timedatectl 已不可用")
            if os.geteuid() != 0:
                sudo = shutil.which("sudo")
                if not sudo:
                    raise RuntimeError("当前用户修改时区需要 sudo，但未找到该程序")
                print("[人工步骤] 仅在终端原生 sudo 隐藏提示中输入系统密码；脚本不会读取或保存密码。")
                subprocess.run([sudo, "-v"], check=True)
                timezone_command = [sudo]
            timezone_command += [executable, "set-timezone", "Asia/Taipei"]
        print("[2/6] 身份轮换：同步 {} 份内部备份".format(rotate_identity(home)) if rotate else "[2/6] 身份保持不变")
        batch = quarantine_root(home) / "claude-cleanup-{}".format(dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        moved = []
        if remove_credentials and credentials in targets:
            moved.append(move_to_trash(credentials, set(targets), claude, batch))
            print("[3/6] CLI 凭据已移入隔离；未显示凭据内容")
        elif remove_credentials:
            print("[3/6] 未检测到 .credentials.json，无需处理；不扫描其他密钥存储")
        else:
            print("[3/6] CLI 凭据保持不变")
        for target in targets:
            if target != credentials:
                moved.append(move_to_trash(target, set(targets), claude, batch))
        print("[4/6] 已移入隔离：{} 项".format(len(moved)))
        if simple_mode:
            update_simple(home, simple_mode)
        print("[5/6] 精简模式：{}".format(("保持", "启用", "移除")[simple_mode]))
        if set_timezone:
            subprocess.run(timezone_command, check=True)
            if current_timezone() != "Asia/Taipei":
                raise RuntimeError("时区修改后验证失败")
        if telemetry(read_json(settings, {})) != before_telemetry:
            raise RuntimeError("遥测设置发生变化，已停止")
        missing = [path for path in protected_before if not _exists(path)]
        if missing:
            raise RuntimeError("受保护路径丢失：" + ", ".join(map(str, missing)))
        print("[6/6] 遥测配置值未变；时区：{}\n\n完成。备份和隔离副本均已保留。\n备份：{}".format(current_timezone(), backup))
        if moved:
            print("隔离批次：{}\n恢复路径对应表：{}".format(batch, batch / "metadata.json"))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, EOFError) as error:
        print("停止：{}".format(error), file=sys.stderr)
        if backup is not None:
            print("已验证备份仍保留：{}".format(backup), file=sys.stderr)
        if batch is not None and batch.exists():
            print("已移动的文件仍在隔离批次中；查看 metadata.json：{}".format(batch), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断；已有备份和隔离副本不会自动删除。", file=sys.stderr)
        if backup is not None:
            print("备份：{}".format(backup), file=sys.stderr)
        if batch is not None and batch.exists():
            print("隔离批次：{}".format(batch), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
