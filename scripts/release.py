#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
import tomllib


RELEASE_FILES = [
    "__init__.py",
    "plugin.yaml",
    "pyproject.toml",
    "README.md",
    "README_ZH.md",
    "BUILDING.md",
    "CHANGELOG.md",
    "src",
    "docs",
]


def log(message: str) -> None:
    print(f"[release] {message}")


def copy_recursive(source_path: Path, target_path: Path) -> None:
    if source_path.is_dir():
        shutil.copytree(source_path, target_path, dirs_exist_ok=True)
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)


def write_sha256(file_path: Path) -> Path:
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    checksum_path = file_path.with_name(f"{file_path.name}.sha256")
    checksum_path.write_text(f"{digest}  {file_path.name}\n", encoding="utf-8")
    return checksum_path


def create_archive(archive_path: Path, source_dir: Path) -> None:
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    output_dir = repo_root / "output"
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    plugin_name = project["name"]
    version = project["version"]
    artifact_base = f"{plugin_name}-v{version}"
    staging_dir = output_dir / artifact_base
    archive_path = output_dir / f"{artifact_base}.tar.gz"
    latest_archive_path = output_dir / f"{plugin_name}.tar.gz"
    installer_path = output_dir / "install.sh"

    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(staging_dir, ignore_errors=True)
    archive_path.unlink(missing_ok=True)
    latest_archive_path.unlink(missing_ok=True)
    Path(f"{archive_path}.sha256").unlink(missing_ok=True)
    Path(f"{latest_archive_path}.sha256").unlink(missing_ok=True)
    installer_path.unlink(missing_ok=True)

    staging_dir.mkdir(parents=True, exist_ok=True)

    for relative in RELEASE_FILES:
        source_path = repo_root / relative
        if not source_path.exists():
            raise FileNotFoundError(f"missing release file: {relative}")
        copy_recursive(source_path, staging_dir / relative)

    (staging_dir / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (staging_dir / "RELEASE.json").write_text(
        json.dumps(
            {
                "name": plugin_name,
                "version": version,
                "builtAt": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    create_archive(archive_path, staging_dir)
    checksum_path = write_sha256(archive_path)
    shutil.copy2(archive_path, latest_archive_path)
    latest_checksum_path = write_sha256(latest_archive_path)

    source_installer = repo_root / "scripts" / "install.sh"
    shutil.copy2(source_installer, installer_path)
    installer_path.chmod(0o755)

    log(f"artifact: {archive_path.relative_to(repo_root)}")
    log(f"checksum: {checksum_path.relative_to(repo_root)}")
    log(f"latest artifact: {latest_archive_path.relative_to(repo_root)}")
    log(f"latest checksum: {latest_checksum_path.relative_to(repo_root)}")
    log(f"installer: {installer_path.relative_to(repo_root)}")


if __name__ == "__main__":
    main()
