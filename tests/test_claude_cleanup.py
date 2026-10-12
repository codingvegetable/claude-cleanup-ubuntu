"""Behavior tests; every home, XDG path, and subprocess is isolated.

Run with Python 3.8 or newer: python3 -m unittest discover -s tests -v
These tests never use the real user's Claude configuration or system settings.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "claude_cleanup.py"
SPEC = importlib.util.spec_from_file_location("claude_cleanup", SCRIPT)
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-cleanup-test-")
        self.addCleanup(temporary.cleanup)
        self.test_home = pathlib.Path(temporary.name)
        self.config = self.test_home / ".claude"
        self.config.mkdir()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {
            "HOME": str(self.test_home),
            "USER": "cleanup-test",
            "LOGNAME": "cleanup-test",
            "XDG_CONFIG_HOME": str(self.test_home / ".config"),
            "XDG_CACHE_HOME": str(self.test_home / ".cache"),
            "XDG_DATA_HOME": str(self.test_home / ".local/share"),
            "XDG_STATE_HOME": str(self.test_home / ".local/state"),
        }, clear=True))
        stack.enter_context(mock.patch.object(pathlib.Path, "home", return_value=self.test_home))
        # An unexpected subprocess must fail locally instead of inspecting the
        # machine, reaching a credential store, invoking sudo, or changing time.
        stack.enter_context(mock.patch.object(
            cleanup.subprocess, "Popen", side_effect=AssertionError("Unexpected real subprocess")))

    def seed_protected(self, config=None):
        config = config or self.config
        files = {
            "projects/session.jsonl": "session",
            "skills/demo/SKILL.md": "skill",
            "plugins/demo.json": "plugin",
            "hooks/guard.sh": "hook",
        }
        for relative, content in files.items():
            target = config / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return files

    def seed_cache(self, config=None):
        cache = (config or self.config) / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "entry").write_text("cached data", encoding="utf-8")
        return cache

    def snapshot(self, root):
        """Record entries without following directory or file symlinks."""
        found = {}
        for directory, folders, files in os.walk(str(root), followlinks=False):
            for name in folders + files:
                path = pathlib.Path(directory) / name
                relative = str(path.relative_to(root))
                mode = stat.S_IMODE(path.lstat().st_mode)
                if path.is_symlink():
                    found[relative] = ("link", os.readlink(str(path)), mode)
                elif path.is_dir():
                    found[relative] = ("directory", mode)
                else:
                    found[relative] = ("file", path.read_bytes(), mode)
        return found

    def run_main(self, answers=(), processes=None, argv=None, under_claude=False):
        prompts = []
        iterator = iter(answers)

        def answer(prompt=""):
            prompts.append(prompt)
            try:
                return next(iterator)
            except StopIteration:
                self.fail("Unexpected additional interactive prompt: " + prompt)

        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cleanup, "running_under_claude", return_value=under_claude))
            stack.enter_context(mock.patch.object(cleanup, "claude_processes", return_value=processes or []))
            stack.enter_context(mock.patch.object(cleanup, "current_timezone", return_value="Asia/Shanghai"))
            stack.enter_context(mock.patch.object(cleanup, "timezone_supported", return_value=False))
            stack.enter_context(mock.patch.object(sys.stdin, "isatty", return_value=True))
            stack.enter_context(mock.patch("builtins.input", side_effect=answer))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(output))
            status = cleanup.main([] if argv is None else argv)
        return status, prompts, output.getvalue()

    def assert_protected_unchanged(self, expected, config=None):
        for relative, content in expected.items():
            self.assertEqual(((config or self.config) / relative).read_text(encoding="utf-8"), content)

    def test_backup_contains_all_claude_data_and_identity(self):
        files = self.seed_protected()
        credentials = self.config / ".credentials.json"
        credentials.write_text('{"token":"test-only"}', encoding="utf-8")
        identity = self.test_home / ".claude.json"
        identity.write_text('{"userID":"old"}', encoding="utf-8")
        backup = cleanup.create_backup(self.test_home)
        backup.relative_to(self.test_home)
        for relative, content in files.items():
            self.assertEqual((backup / "dot-claude" / relative).read_text(encoding="utf-8"), content)
        self.assertEqual((backup / "dot-claude/.credentials.json").read_bytes(), credentials.read_bytes())
        self.assertEqual((backup / "claude.json").read_bytes(), identity.read_bytes())
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(manifest["files"], len(files) + 1)
        self.assertTrue(manifest["claudeJsonCopied"])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)

    def test_deletion_gate_rejects_protected_and_unknown_paths(self):
        batch = self.test_home / "quarantine/test-run"
        for name in cleanup.PROTECTED:
            with self.subTest(name=name):
                target = self.config / name
                with self.assertRaises(RuntimeError):
                    cleanup.move_to_trash(target, {target}, self.config, batch)
        unknown = self.test_home / "Documents"
        unknown.mkdir()
        (unknown / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            cleanup.move_to_trash(unknown, set(), self.config, batch)
        self.assertEqual((unknown / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_safe_cache_moves_without_touching_project(self):
        protected = self.seed_protected()
        cache = self.seed_cache()
        destination = cleanup.move_to_trash(cache, {cache}, self.config, self.test_home / "quarantine/run")
        self.assertFalse(cache.exists())
        self.assertEqual((destination / "entry").read_text(encoding="utf-8"), "cached data")
        self.assert_protected_unchanged(protected)

    def test_identity_rotation_syncs_internal_backups(self):
        identity = self.test_home / ".claude.json"
        cleanup.write_json(identity, {"userID": "old", "machineID": "old", "oauthAccount": {}, "keep": 1})
        internal = self.config / "backups/.claude.json.backup.1"
        internal.parent.mkdir()
        cleanup.write_json(internal, {"userID": "old", "machineID": "old", "oauthAccount": {}, "keepBackup": 1})
        self.assertEqual(cleanup.rotate_identity(self.test_home), 1)
        main, backup = cleanup.read_json(identity), cleanup.read_json(internal)
        self.assertNotEqual(main["userID"], "old")
        self.assertNotEqual(main["machineID"], "old")
        self.assertEqual((main["userID"], main["machineID"]), (backup["userID"], backup["machineID"]))
        self.assertNotIn("oauthAccount", main)
        self.assertNotIn("oauthAccount", backup)
        self.assertEqual((main["keep"], backup["keepBackup"]), (1, 1))

    def test_simple_mode_preserves_telemetry_and_every_other_setting(self):
        path = self.config / "settings.json"
        original = {
            "env": {
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://example.invalid:4317",
                "ANTHROPIC_BASE_URL": "https://example.invalid",
                "ANTHROPIC_AUTH_TOKEN": "test-only-token",
                "TOKEN": "test-only-secret",
            },
            "hooks": {"x": ["keep"]},
            "permissions": {"deny": ["Bash(rm *)"]},
            "unknownSetting": {"nested": [1, 2, 3]},
        }
        cleanup.write_json(path, original)
        for value in (1, 2):
            with self.subTest(value=value):
                cleanup.write_json(path, original)
                cleanup.update_simple(self.test_home, value)
                updated = cleanup.read_json(path)
                if value == 1:
                    self.assertEqual(updated["env"]["CLAUDE_CODE_SIMPLE"], "1")
                    del updated["env"]["CLAUDE_CODE_SIMPLE"]
                else:
                    self.assertNotIn("CLAUDE_CODE_SIMPLE", updated["env"])
                self.assertEqual(cleanup.telemetry(updated), cleanup.telemetry(original))
                self.assertEqual(updated, original)

    def test_safe_cleanup_is_not_a_question_and_cancel_writes_nothing(self):
        cache = self.seed_cache()
        before = self.snapshot(self.test_home)
        status, prompts, _ = self.run_main(["n", "n", "0", "0", "NO"])
        self.assertEqual(status, 1)
        self.assertTrue(cache.exists())
        self.assertEqual(self.snapshot(self.test_home), before)
        self.assertEqual(len(prompts), 5)
        self.assertFalse(any("自动清理" in prompt or "可再生缓存" in prompt for prompt in prompts))

    def test_running_claude_skips_safe_cleanup_without_exit_prompt(self):
        cache = self.seed_cache()
        status, prompts, _ = self.run_main(["n", "n", "0", "0", "CONFIRM"], processes=["123 claude"])
        self.assertEqual(status, 0)
        self.assertTrue(cache.exists())
        self.assertFalse(any("退出后按 Enter" in prompt for prompt in prompts))

    def test_claude_agent_allows_audit_only_without_writes(self):
        self.seed_cache()
        before = self.snapshot(self.test_home)
        status, prompts, _ = self.run_main(under_claude=True)
        self.assertEqual(status, 2)
        self.assertFalse(prompts)
        self.assertEqual(self.snapshot(self.test_home), before)
        status, prompts, _ = self.run_main(argv=["--audit"], under_claude=True)
        self.assertEqual(status, 0)
        self.assertFalse(prompts)
        self.assertEqual(self.snapshot(self.test_home), before)

    def test_isolated_full_run_preserves_protected_data_and_telemetry(self):
        protected = self.seed_protected()
        settings = {"env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "TOKEN": "test-token"}, "hooks": {"x": ["keep"]}}
        cleanup.write_json(self.config / "settings.json", settings)
        cleanup.write_json(self.test_home / ".claude.json", {"userID": "old", "machineID": "old", "oauthAccount": {}, "keep": True})
        internal = self.config / "backups/.claude.json.backup.1"
        internal.parent.mkdir()
        cleanup.write_json(internal, {"userID": "old", "machineID": "old", "backupKeep": True})
        cache = self.seed_cache()
        status, _, output = self.run_main(["y", "n", "0", "1", "CONFIRM"])
        self.assertEqual(status, 0, output)
        backups = list((self.test_home / "ClaudeBackups").glob("claude-cleanup-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "dot-claude/projects/session.jsonl").exists())
        self.assertFalse(cache.exists())
        self.assert_protected_unchanged(protected)
        final = cleanup.read_json(self.config / "settings.json")
        self.assertEqual(cleanup.telemetry(final), cleanup.telemetry(settings))
        self.assertEqual(final["hooks"], settings["hooks"])
        self.assertEqual(final["env"]["TOKEN"], "test-token")
        identity, saved = cleanup.read_json(self.test_home / ".claude.json"), cleanup.read_json(internal)
        self.assertEqual(identity["userID"], saved["userID"])
        self.assertTrue(identity["keep"] and saved["backupKeep"])

    def test_all_six_upstream_cache_entries_are_automatically_cleaned(self):
        names = ("cache", "stats-cache.json", "telemetry", "usage-data", "usage.jsonl", "usage.with-fix.jsonl")
        original = {}
        for name in names:
            target = self.config / name
            if name in ("cache", "telemetry", "usage-data"):
                target.mkdir()
                (target / "entry").write_text(name, encoding="utf-8")
            else:
                target.write_text(name, encoding="utf-8")
            original[name] = target
        unknown = self.config / "unlisted-cache"
        unknown.mkdir()
        (unknown / "keep").write_text("unknown data", encoding="utf-8")
        status, _, output = self.run_main(["n", "n", "0", "0", "CONFIRM"])
        self.assertEqual(status, 0, output)
        for name, target in original.items():
            with self.subTest(name=name):
                self.assertFalse(target.exists())
        self.assertEqual((unknown / "keep").read_text(encoding="utf-8"), "unknown data")

    def test_linux_xdg_paths_and_desktop_selection(self):
        xdg_config = self.test_home / "custom-xdg/config"
        xdg_cache = self.test_home / "custom-xdg/cache"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_config), "XDG_CACHE_HOME": str(xdg_cache)}):
            cli_cache = xdg_cache / "claude-cli-nodejs"
            cli_cache.mkdir(parents=True)
            (cli_cache / "entry").write_text("cli cache", encoding="utf-8")
            desktop_data, desktop_cache = [], []
            for root, paths in ((xdg_config, desktop_data), (xdg_cache, desktop_cache)):
                for name in ("Claude", "Claude-3p", "claude-desktop"):
                    target = root / name
                    target.mkdir(parents=True)
                    (target / "entry").write_text("desktop " + name, encoding="utf-8")
                    paths.append(target)
            unrelated = xdg_config / "claude-not-allowed"
            unrelated.mkdir()
            (unrelated / "keep").write_text("other application", encoding="utf-8")
            status, _, output = self.run_main(["n", "n", "0", "0", "CONFIRM"])
            self.assertEqual(status, 0, output)
            self.assertFalse(cli_cache.exists())
            for path in desktop_cache:
                self.assertFalse(path.exists())
            for path in desktop_data:
                self.assertTrue(path.exists())
            status, _, output = self.run_main(["n", "n", "1", "0", "CONFIRM"])
            self.assertEqual(status, 0, output)
            for path in desktop_data:
                self.assertFalse(path.exists())
            self.assertEqual((unrelated / "keep").read_text(encoding="utf-8"), "other application")

    def test_credentials_are_opt_in_and_moved_with_recovery_metadata(self):
        credentials = self.config / ".credentials.json"
        original = b'{"claudeAiOauth":{"accessToken":"test-only-credential"}}\n'
        credentials.write_bytes(original)
        status, _, output = self.run_main(["n", "n", "0", "0", "CONFIRM"])
        self.assertEqual(status, 0, output)
        self.assertEqual(credentials.read_bytes(), original)
        status, _, output = self.run_main(["n", "y", "0", "0", "CONFIRM"])
        self.assertEqual(status, 0, output)
        self.assertFalse(credentials.exists())
        quarantine = cleanup.quarantine_root(self.test_home)
        quarantine.relative_to(self.test_home)
        matches = []
        for metadata in quarantine.glob("*/metadata.json"):
            raw = metadata.read_text(encoding="utf-8")
            self.assertNotIn("test-only-credential", raw)
            data = json.loads(raw)
            entries = data["entries"] if isinstance(data, dict) else data
            for entry in entries:
                if entry["original"] == str(credentials):
                    destination = pathlib.Path(entry["quarantine"])
                    destination.relative_to(quarantine)
                    self.assertEqual(destination.read_bytes(), original)
                    self.assertTrue(entry["moved"])
                    matches.append(destination)
        self.assertEqual(len(matches), 1)

    def test_claude_config_dir_is_used_for_backup_cleanup_and_identity(self):
        default_cache = self.seed_cache()
        default_settings = self.config / "settings.json"
        cleanup.write_json(default_settings, {"env": {"TOKEN": "default-token"}})
        (self.config / ".credentials.json").write_text("default credential", encoding="utf-8")
        default_identity = self.test_home / ".claude.json"
        cleanup.write_json(default_identity, {"userID": "default", "keepDefault": True})
        custom = self.test_home / "custom-claude"
        protected = self.seed_protected(custom)
        custom_cache = self.seed_cache(custom)
        custom_identity = custom / ".claude.json"
        cleanup.write_json(custom_identity, {"userID": "custom", "machineID": "custom", "oauthAccount": {}, "keepCustom": True})
        cleanup.write_json(custom / "settings.json", {"env": {"TOKEN": "custom-token"}})
        custom_credentials = custom / ".credentials.json"
        custom_credentials.write_text("custom credential", encoding="utf-8")
        internal = custom / "backups/.claude.json.backup.1"
        internal.parent.mkdir()
        cleanup.write_json(internal, {"userID": "custom", "machineID": "custom", "keepBackup": True})
        default_before = self.snapshot(self.config)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom)}):
            self.assertEqual(cleanup.claude_config_dir(self.test_home), custom)
            self.assertEqual(cleanup.identity_path(self.test_home), custom_identity)
            status, _, output = self.run_main(["y", "y", "0", "1", "CONFIRM"])
        self.assertEqual(status, 0, output)
        self.assertFalse(custom_cache.exists())
        self.assertFalse(custom_credentials.exists())
        self.assertTrue(default_cache.exists())
        self.assertEqual(self.snapshot(self.config), default_before)
        self.assertEqual(cleanup.read_json(default_identity), {"userID": "default", "keepDefault": True})
        self.assert_protected_unchanged(protected, custom)
        selected = cleanup.read_json(custom_identity)
        self.assertNotEqual(selected["userID"], "custom")
        self.assertEqual(selected["userID"], cleanup.read_json(internal)["userID"])
        self.assertTrue(selected["keepCustom"])
        self.assertEqual(cleanup.read_json(custom / "settings.json")["env"], {"TOKEN": "custom-token", "CLAUDE_CODE_SIMPLE": "1"})
        backups = list((self.test_home / "ClaudeBackups").glob("claude-cleanup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(cleanup.read_json(backups[0] / "claude.json")["userID"], "custom")
        self.assertEqual(cleanup.read_json(backups[0] / "home-claude.json")["userID"], "default")

    def test_corrupt_backup_prevents_cleanup_and_configuration_changes(self):
        self.seed_cache()
        identity = self.test_home / ".claude.json"
        cleanup.write_json(identity, {"userID": "old", "machineID": "old", "keep": True})
        cleanup.write_json(self.config / "settings.json", {"env": {"TOKEN": "test-token"}})
        credentials = self.config / ".credentials.json"
        credentials.write_text("test credential", encoding="utf-8")
        config_before = self.snapshot(self.config)
        identity_before = identity.read_bytes()
        real_copytree = cleanup.shutil.copytree

        def corrupt_copytree(source, destination, *args, **kwargs):
            copied = real_copytree(source, destination, *args, **kwargs)
            if pathlib.Path(source) == self.config:
                (pathlib.Path(destination) / "cache/entry").write_text("corrupted backup", encoding="utf-8")
            return copied

        with mock.patch.object(cleanup.shutil, "copytree", side_effect=corrupt_copytree):
            status, _, _ = self.run_main(["y", "y", "0", "1", "CONFIRM"])
        self.assertNotEqual(status, 0)
        self.assertEqual(self.snapshot(self.config), config_before)
        self.assertEqual(identity.read_bytes(), identity_before)
        quarantine = cleanup.quarantine_root(self.test_home)
        quarantine.relative_to(self.test_home)
        self.assertFalse(quarantine.exists())

    def test_audit_never_writes_or_asks_for_confirmation(self):
        self.seed_cache()
        (self.config / ".credentials.json").write_text("test credential", encoding="utf-8")
        before = self.snapshot(self.test_home)
        status, prompts, output = self.run_main(argv=["--audit"])
        self.assertEqual(status, 0, output)
        self.assertFalse(prompts)
        self.assertEqual(self.snapshot(self.test_home), before)

    def test_symlink_targets_and_ancestors_cannot_escape_the_cleanup_root(self):
        outside = self.test_home / "outside"
        outside.mkdir()
        marker = outside / "keep"
        marker.write_text("must survive", encoding="utf-8")
        linked_cache = self.config / "cache"
        linked_cache.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            cleanup.move_to_trash(linked_cache, {linked_cache}, self.config, self.test_home / "quarantine/run")
        self.assertTrue(linked_cache.is_symlink())
        self.assertEqual(marker.read_text(encoding="utf-8"), "must survive")
        linked_cache.unlink()
        parent = self.test_home / "linked-parent"
        parent.symlink_to(outside, target_is_directory=True)
        target = parent / "cache"
        target.mkdir()
        (target / "entry").write_text("external cache", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            cleanup.move_to_trash(target, {target}, self.config, self.test_home / "quarantine/run")
        self.assertEqual((outside / "cache/entry").read_text(encoding="utf-8"), "external cache")
        self.assertEqual(marker.read_text(encoding="utf-8"), "must survive")
        safe_cache = self.seed_cache()
        linked_quarantine = self.test_home / "linked-quarantine"
        linked_quarantine.symlink_to(outside, target_is_directory=True)
        before = self.snapshot(outside)
        with self.assertRaises(RuntimeError):
            cleanup.move_to_trash(safe_cache, {safe_cache}, self.config, linked_quarantine / "run")
        self.assertTrue(safe_cache.exists())
        self.assertEqual(self.snapshot(outside), before)

    def test_protected_root_alias_blocks_cleanup_of_its_cache_target(self):
        cache = self.seed_cache()
        live_data = cache / "live-data"
        live_data.mkdir()
        session = live_data / "session.jsonl"
        session.write_text("protected conversation", encoding="utf-8")
        projects = self.config / "projects"
        projects.symlink_to(live_data, target_is_directory=True)
        before = self.snapshot(self.config)
        with self.assertRaises(RuntimeError):
            cleanup.move_to_trash(cache, {cache}, self.config, self.test_home / "quarantine/run")
        status, _, output = self.run_main(["n", "n", "0", "0", "CONFIRM"])
        self.assertNotEqual(status, 0, output)
        self.assertEqual(self.snapshot(self.config), before)
        self.assertEqual(session.read_text(encoding="utf-8"), "protected conversation")
        self.assertEqual((projects / "session.jsonl").read_text(encoding="utf-8"), "protected conversation")

    def test_nested_protected_alias_blocks_cleanup_of_its_cache_target(self):
        cache = self.seed_cache()
        assets = cache / "assets"
        assets.mkdir()
        skill_file = assets / "SKILL.md"
        skill_file.write_text("protected skill content", encoding="utf-8")
        skills = self.config / "skills"
        skills.mkdir()
        linked_skill = skills / "demo"
        linked_skill.symlink_to(assets, target_is_directory=True)
        before = self.snapshot(self.config)
        with self.assertRaises(RuntimeError):
            cleanup.move_to_trash(cache, {cache}, self.config, self.test_home / "quarantine/run")
        status, _, output = self.run_main(["n", "n", "0", "0", "CONFIRM"])
        self.assertNotEqual(status, 0, output)
        self.assertEqual(self.snapshot(self.config), before)
        self.assertEqual(skill_file.read_text(encoding="utf-8"), "protected skill content")
        self.assertEqual((linked_skill / "SKILL.md").read_text(encoding="utf-8"), "protected skill content")

    def test_backup_preserves_symlinks_without_copying_external_data(self):
        outside = self.test_home / "external-secret.txt"
        outside.write_text("external-only-content", encoding="utf-8")
        link = self.config / "linked-file"
        link.symlink_to(outside)
        backup = cleanup.create_backup(self.test_home)
        copied_link = backup / "dot-claude/linked-file"
        self.assertTrue(copied_link.is_symlink())
        self.assertEqual(os.readlink(str(copied_link)), str(outside))
        self.assertEqual(outside.read_text(encoding="utf-8"), "external-only-content")
        for directory, _, files in os.walk(str(backup), followlinks=False):
            for name in files:
                path = pathlib.Path(directory) / name
                if not path.is_symlink():
                    self.assertNotIn(b"external-only-content", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
