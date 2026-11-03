from pathlib import Path
import tempfile
import unittest

from scripts.release import copy_recursive, validate_release_version


class ReleaseTests(unittest.TestCase):
    def test_release_rejects_manifest_or_runtime_version_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            for manifest, runtime in [("0.1.10", "0.1.10"), ("0.1.9", "0.1.10"), ("0.1.10", "0.1.9")]:
                with self.subTest(manifest=manifest, runtime=runtime):
                    (root / "plugin.yaml").write_text(f"version: {manifest}\n")
                    (root / "src/__init__.py").write_text(f'PLUGIN_VERSION = "{runtime}"\n')
                    if manifest == runtime == "0.1.10":
                        validate_release_version(root, "0.1.10")
                    else:
                        with self.assertRaises(ValueError):
                            validate_release_version(root, "0.1.10")

    def test_release_excludes_python_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            (source / "__pycache__").mkdir(parents=True)
            (source / "__pycache__/plugin.pyc").write_bytes(b"cache")
            (source / "plugin.py").write_text("# source\n")
            target = root / "release"
            copy_recursive(source, target)
            self.assertTrue((target / "plugin.py").is_file())
            self.assertFalse((target / "__pycache__").exists())
