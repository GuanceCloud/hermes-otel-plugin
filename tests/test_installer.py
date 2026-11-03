from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import venv

import yaml


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "The release installer uses Bash")
class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="hermes installer ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        venv.EnvBuilder(with_pip=False).create(self.runtime)
        self.python = self.runtime / "bin/python"
        self.site = Path(subprocess.check_output(
            [str(self.python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            text=True,
        ).strip())
        self.yaml_source = Path(yaml.__file__).parent
        self.home = self.root / "data/hermes data"
        self.home.mkdir(parents=True)
        self.config = self.home / "config.yaml"
        self.config.write_text("model: preserved\nplugins:\n  enabled: [other-plugin]\nhermes_otel_plugin:\n  headers:\n    Custom: kept\n", encoding="utf-8")
        payload = self.root / "payload"
        (payload / "src").mkdir(parents=True)
        for name in ["plugin.yaml", "__init__.py", "src/plugin.py"]:
            (payload / name).write_text("", encoding="utf-8")
        shutil.copy(ROOT / "pyproject.toml", payload / "pyproject.toml")
        self.archive = self.root / "plugin.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            archive.add(payload, arcname="plugin")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Simulate a host python3 without PyYAML. No installer operation should
        # use this interpreter when Hermes has its own runtime.
        self.host_calls = self.root / "host-python-calls"
        host = self.bin / "python3"
        host.write_text('#!/bin/sh\nprintf used >> "$HOST_PYTHON_CALLS"\nexit 97\n')
        host.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.root / "home"),
                        HERMES_HOME=str(self.home), HERMES_PYTHON=str(self.python),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        HOST_PYTHON_CALLS=str(self.host_calls))
        for key in ["HERMES_PLUGIN_DIR", "HERMES_CONFIG_FILE", "HERMES_PLUGIN_NAME"]:
            self.env.pop(key, None)

    def run_installer(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(ROOT / "scripts/install.sh"), str(self.archive),
                               "--no-restart", *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def provide_yaml(self) -> None:
        shutil.copytree(self.yaml_source, self.site / "yaml")

    def test_configuration_uses_hermes_runtime_not_host_python(self) -> None:
        self.provide_yaml()
        result = self.run_installer("--no-deps", "--endpoint", "https://example.invalid",
                                    "--x-token", "test-token", "--tag", "env=testing")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.host_calls.exists())
        config = yaml.safe_load(self.config.read_text())
        self.assertEqual(config["model"], "preserved")
        self.assertEqual(config["plugins"]["enabled"], ["other-plugin", "hermes-otel-plugin"])
        section = config["hermes_otel_plugin"]
        self.assertEqual(section["endpoint"], "https://example.invalid")
        self.assertEqual(section["headers"]["Custom"], "kept")
        self.assertEqual(section["headers"]["X-Token"], "test-token")
        self.assertEqual(section["resource_attributes"]["env"], "testing")

    def test_default_install_bootstraps_yaml_in_hermes_runtime(self) -> None:
        # A local pip double makes the test offline while verifying both the
        # dependency declaration and the selected interpreter for installation.
        pip = self.site / "pip"
        pip.mkdir()
        (pip / "__init__.py").write_text("")
        (pip / "__main__.py").write_text('''import json, os, pathlib, shutil, sys
assert "PyYAML>=6.0,<7" in sys.argv, sys.argv
pathlib.Path(os.environ["PIP_CALL"]).write_text(json.dumps({"python": sys.executable, "args": sys.argv}))
shutil.copytree(os.environ["YAML_SOURCE"], pathlib.Path(__file__).parent.parent / "yaml")
''')
        call = self.root / "pip-call.json"
        self.env.update(PIP_CALL=str(call), YAML_SOURCE=str(self.yaml_source))
        result = self.run_installer("--endpoint", "https://example.invalid", "--x-token", "test-token")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(call.read_text())["python"], str(self.python))
        self.assertTrue(yaml.safe_load(self.config.read_text())["hermes_otel_plugin"]["enabled"])
        self.assertFalse(self.host_calls.exists())

    def test_no_config_update_preserves_configuration_without_yaml(self) -> None:
        before = self.config.read_bytes()
        result = self.run_installer("--no-config", "--no-deps")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.host_calls.exists())

    def test_missing_runtime_yaml_leaves_configuration_unchanged(self) -> None:
        before = self.config.read_bytes()
        result = self.run_installer("--no-deps", "--endpoint", "https://example.invalid", "--x-token", "test-token")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PyYAML", result.stderr)
        self.assertIn(str(self.python), result.stderr)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.host_calls.exists())

    def test_invalid_yaml_does_not_replace_existing_configuration(self) -> None:
        self.provide_yaml()
        self.config.write_text("plugins: [broken\n")
        before = self.config.read_bytes()
        result = self.run_installer("--no-deps", "--endpoint", "https://example.invalid", "--x-token", "test-token")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_config_symlink_is_preserved(self) -> None:
        self.provide_yaml()
        target = self.home / "actual-config.yaml"
        self.config.rename(target)
        self.config.symlink_to(target)
        original_mode = target.stat().st_mode & 0o777
        result = self.run_installer("--no-deps", "--endpoint", "https://example.invalid", "--x-token", "test-token")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, original_mode)
        self.assertEqual(yaml.safe_load(target.read_text())["model"], "preserved")

    def test_missing_runtime_fails_before_replacing_plugin(self) -> None:
        self.env["HERMES_PYTHON"] = str(self.root / "missing-python")
        result = self.run_installer("--endpoint", "https://example.invalid", "--x-token", "test-token")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("set HERMES_PYTHON", result.stderr)
        self.assertFalse((self.home / "plugins/hermes-otel-plugin").exists())
