from __future__ import annotations

from pathlib import Path
import re
import subprocess

AGENT_RUNTIME = "hermes"
PLUGIN_VERSION = "0.1.5"


def _resolve_agent_version() -> str:
    candidate = Path.home() / ".hermes" / "hermes-agent" / "hermes_cli" / "__init__.py"
    try:
        content = candidate.read_text(encoding="utf-8")
    except Exception:
        content = ""
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    if match:
        normalized = match.group(1).strip()
        if normalized:
            return normalized

    try:
        completed = subprocess.run(
            ["hermes", "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        cli_match = re.search(r"Hermes Agent v([0-9][^\s]*)", output)
        if cli_match:
            normalized = cli_match.group(1).strip()
            if normalized:
                return normalized
    except Exception:
        pass

    try:
        from hermes_cli import __version__ as hermes_version

        normalized = str(hermes_version).strip()
        if normalized:
            return normalized
    except Exception:
        pass
    return "unknown"


AGENT_VERSION = _resolve_agent_version()

from .plugin import HermesOtelPlugin, register

__all__ = ["AGENT_RUNTIME", "AGENT_VERSION", "HermesOtelPlugin", "PLUGIN_VERSION", "register"]
