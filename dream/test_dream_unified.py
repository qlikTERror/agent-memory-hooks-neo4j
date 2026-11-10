"""Offline startup and fallback regressions; no LLM or database requests."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import dream_unified as dream


class CopilotDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "PATH": str(self.root),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "APPDATA": str(self.root / "appdata"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bin = patch.object(dream, "COPILOT_BIN", None)
        self.bin.start()
        self.addCleanup(self.bin.stop)

    def test_path_shim_without_ps1_in_pathext(self):
        shim = self.root / "copilot.ps1"
        shim.touch()
        original_pathext = os.environ["PATHEXT"]
        command = dream._detect_copilot_command()
        self.assertEqual(command, [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(shim),
        ])
        self.assertEqual(os.environ["PATHEXT"], original_pathext)

    def test_path_order_and_quoted_directory_with_spaces(self):
        first = self.root / "first directory"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        (first / "copilot.ps1").touch()
        (second / "copilot.ps1").touch()
        with patch.dict(os.environ, {"PATH": f'"{first}"{os.pathsep}{second}'}):
            self.assertEqual(dream._detect_copilot_command()[-1],
                             str(first / "copilot.ps1"))

    def test_explicit_ps1_takes_precedence(self):
        with patch.object(dream, "COPILOT_BIN", "custom.PS1"):
            self.assertEqual(dream._detect_copilot_command()[-1], "custom.PS1")

    def test_explicit_cmd_is_rejected(self):
        with patch.object(dream, "COPILOT_BIN", "copilot.cmd"):
            with self.assertRaisesRegex(RuntimeError, "must point to a .ps1"):
                dream._detect_copilot_command()

    def test_vscode_fallback(self):
        shim = (self.root / "appdata" / "Code" / "User" / "globalStorage"
                / "github.copilot-chat" / "copilotCli" / "copilot.ps1")
        shim.parent.mkdir(parents=True)
        shim.touch()
        self.assertEqual(dream._detect_copilot_command()[-1], str(shim))

    def test_cmd_and_bat_shims_do_not_hide_missing_ps1(self):
        (self.root / "copilot.cmd").touch()
        (self.root / "copilot.bat").touch()
        with self.assertRaisesRegex(RuntimeError, "Could not find copilot.ps1"):
            dream._detect_copilot_command()


class StartupAndFallbackTests(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(self.stderr))
        unavailable = patch.object(dream, "UNAVAILABLE_BACKENDS_THIS_RUN", set())
        unavailable.start()
        self.addCleanup(unavailable.stop)

    def test_main_does_not_initialize_unused_copilot_fallback(self):
        driver = Mock()
        claude = Mock(return_value=[])
        copilot = Mock(side_effect=RuntimeError("missing copilot"))
        with (
            patch.object(sys, "argv", ["dream_unified.py", "--backend",
                                      "claude", "copilot", "--dry-run"]),
            patch("dream_logging.setup_logging") as logging_setup,
            patch.object(dream, "_get_copilot_mcp_disable_args",
                         side_effect=RuntimeError("missing copilot")) as mcp,
            patch.object(dream, "get_driver", return_value=driver),
            patch.object(dream, "fetch_events",
                         return_value=[("session", [{"timestamp": "now"}])]),
            patch.object(dream, "fetch_existing_memories", return_value=[]),
            patch.object(dream, "render_events", return_value="events"),
            patch.dict(dream.BACKEND_MAP, claude=claude, copilot=copilot),
        ):
            claude.side_effect = lambda *a, **kw: (
                logging_setup.assert_called_once() or []
            )
            dream.main()
        claude.assert_called_once()
        copilot.assert_not_called()
        mcp.assert_not_called()
        driver.close.assert_called_once()

    def test_model_discovery_failure_uses_next_backend(self):
        claude = Mock(return_value=[])
        copilot = Mock()

        def resolve(backend, model):
            if backend == "copilot":
                raise RuntimeError("Could not find copilot.ps1")
            return model

        with (
            patch.object(dream, "_resolve_model", side_effect=resolve),
            patch.dict(dream.BACKEND_MAP, claude=claude, copilot=copilot),
        ):
            self.assertEqual(dream.call_with_fallback(
                ["copilot-gpt-mini", "claude"], "events", ""), [])
        claude.assert_called_once()
        copilot.assert_not_called()

    def test_copilot_initializes_mcp_flags_lazily_and_caches_them(self):
        response = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"type":"assistant.message","data":{"content":"done"}}\n',
            stderr="",
        )
        with (
            patch.object(dream, "_COPILOT_MCP_DISABLE_ARGS", None),
            patch.object(dream, "_detect_copilot_command",
                         return_value=["powershell", "-File", "copilot.ps1"]),
            patch.object(dream, "_discover_copilot_mcp_server_names",
                         return_value=["example"]) as discover,
            patch.object(dream.subprocess, "run", return_value=response) as run,
        ):
            discover.assert_not_called()
            for _ in range(2):
                self.assertEqual(dream._call_copilot_cli("prompt"), "done")
            discover.assert_called_once()
            for call in run.call_args_list:
                command = call.args[0]
                self.assertIn("--disable-builtin-mcps", command)
                index = command.index("--disable-mcp-server")
                self.assertEqual(command[index + 1], "example")

    def test_missing_copilot_fallback_is_explicit(self):
        with patch.dict(
            dream.BACKEND_MAP,
            claude=Mock(side_effect=RuntimeError("Claude unavailable")),
            copilot=Mock(side_effect=RuntimeError("Could not find copilot.ps1")),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "all backends failed.*Could not find copilot.ps1"
            ):
                dream.call_with_fallback(["claude", "copilot"], "events", "")
        self.assertIn("Claude unavailable", self.stderr.getvalue())

    def test_rate_limited_backend_stays_skipped_for_this_run(self):
        copilot = Mock(side_effect=dream.BackendRateLimitedError(
            "copilot", 429, "rate limited"))
        claude = Mock(return_value=[])
        with patch.dict(dream.BACKEND_MAP, copilot=copilot, claude=claude):
            for _ in range(2):
                dream.call_with_fallback(["copilot", "claude"], "events", "")
        self.assertEqual(copilot.call_count, 1)
        self.assertEqual(claude.call_count, 2)

    def test_startup_error_is_written_to_rotated_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "dream.log"
            proc = subprocess.run(
                [sys.executable, str(Path(dream.__file__)),
                 "--backend", "claude", "copilot", "--since", "invalid",
                 "--log-file", str(log_file)],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertNotEqual(proc.returncode, 0)
            logs = list(Path(directory).glob("dream.*.*.log"))
            self.assertEqual(len(logs), 1)
            content = logs[0].read_text(encoding="utf-8")
            self.assertIn("ValueError", content)
            self.assertIn("--since must look like", content)
            self.assertNotIn("Could not find copilot.ps1", content)


if __name__ == "__main__":
    unittest.main()
