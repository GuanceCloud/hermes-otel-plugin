#!/usr/bin/env bash
set -euo pipefail

PLUGIN_NAME="${HERMES_PLUGIN_NAME:-hermes-otel-plugin}"
OSS_ENDPOINT="${OSS_ENDPOINT:-}"
DOWNLOAD_BASE_URL=""
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="${HERMES_PLUGIN_DIR:-$HERMES_HOME_DIR/plugins/${PLUGIN_NAME}}"
CONFIG_FILE="${HERMES_CONFIG_FILE:-$HERMES_HOME_DIR/config.yaml}"
HERMES_PYTHON="${HERMES_PYTHON:-$HERMES_HOME_DIR/hermes-agent/venv/bin/python}"
RESTART_GATEWAY=1
WRITE_CONFIG=1
INSTALL_DEPS=1
VERSION_INPUT=""
ENDPOINT=""
INSTALL_TYPE="${HERMES_PLUGIN_INSTALL_TYPE:-gtrace}"
X_TOKEN=""
SERVICE_NAME="${HERMES_PLUGIN_SERVICE_NAME:-}"
TAGS=()
tmp_dir=""

log() {
  printf '[install] %s\n' "$1"
}

cleanup() {
  if [ -n "${tmp_dir:-}" ] && [ -d "${tmp_dir:-}" ]; then
    rm -rf "$tmp_dir"
  fi
}

trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage:
  OSS_ENDPOINT=https://example.com scripts/install.sh [latest|vX.Y.Z|X.Y.Z|/path/to/archive.tar.gz|https://...tar.gz] [--type gtrace|otlp] [--endpoint URL] [--x-token TOKEN] [--tag KEY=VALUE] [--service-name NAME] [--no-config] [--no-deps] [--no-restart]

Environment variables:
  OSS_ENDPOINT                 Release root endpoint. Required for latest/version installs.
                               The script appends /hermes-otel-plugin when needed.
  HERMES_HOME                  Hermes home directory. Default: ~/.hermes
  HERMES_PLUGIN_DIR            Install directory. Default: ~/.hermes/plugins/hermes-otel-plugin
  HERMES_CONFIG_FILE           Hermes config file. Default: ~/.hermes/config.yaml
  HERMES_PYTHON                Hermes runtime python. Default: ~/.hermes/hermes-agent/venv/bin/python
  HERMES_PLUGIN_NAME           Plugin package name prefix. Default: hermes-otel-plugin
  HERMES_PLUGIN_INSTALL_TYPE   Install config type. Default: gtrace. Can be set to otlp
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-restart)
      RESTART_GATEWAY=0
      ;;
    --no-config)
      WRITE_CONFIG=0
      ;;
    --no-deps)
      INSTALL_DEPS=0
      ;;
    --oss-endpoint)
      shift
      if [ "$#" -eq 0 ]; then
        printf '[install] --oss-endpoint requires a URL\n' >&2
        exit 1
      fi
      OSS_ENDPOINT="$1"
      ;;
    --oss-endpoint=*)
      OSS_ENDPOINT="${1#*=}"
      ;;
    --endpoint)
      shift
      if [ "$#" -eq 0 ]; then
        printf '[install] --endpoint requires a URL\n' >&2
        exit 1
      fi
      ENDPOINT="$1"
      ;;
    --endpoint=*)
      ENDPOINT="${1#*=}"
      ;;
    --x-token)
      shift
      if [ "$#" -eq 0 ]; then
        printf '[install] --x-token requires a TOKEN\n' >&2
        exit 1
      fi
      X_TOKEN="$1"
      ;;
    --x-token=*)
      X_TOKEN="${1#*=}"
      ;;
    --type)
      shift
      if [ "$#" -eq 0 ]; then
        printf '[install] --type requires a type\n' >&2
        exit 1
      fi
      INSTALL_TYPE="$1"
      ;;
    --type=*)
      INSTALL_TYPE="${1#*=}"
      ;;
    type=*)
      INSTALL_TYPE="${1#*=}"
      ;;
    --service-name)
      shift
      if [ "$#" -eq 0 ]; then
        printf '[install] --service-name requires a NAME\n' >&2
        exit 1
      fi
      SERVICE_NAME="$1"
      ;;
    --service-name=*)
      SERVICE_NAME="${1#*=}"
      ;;
    --tag)
      shift
      if [ "$#" -eq 0 ]; then
        printf '[install] --tag requires KEY=VALUE\n' >&2
        exit 1
      fi
      TAGS+=("$1")
      ;;
    --tag=*)
      TAGS+=("${1#*=}")
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [ -z "$VERSION_INPUT" ]; then
        VERSION_INPUT="$1"
      else
        printf '[install] unexpected argument: %s\n' "$1" >&2
        exit 1
      fi
      ;;
  esac
  shift
done

if [ -z "$VERSION_INPUT" ]; then
  VERSION_INPUT="latest"
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '[install] missing command: %s\n' "$1" >&2
    exit 1
  fi
}

normalize_install_type() {
  case "$1" in
    ""|gtrace)
      printf 'gtrace'
      ;;
    otlp|otel)
      printf 'otlp'
      ;;
    *)
      printf '[install] unsupported --type: %s. Supported values: gtrace, otlp\n' "$1" >&2
      exit 1
      ;;
  esac
}

normalize_version() {
  local value="$1"
  value="${value#v}"
  printf '%s' "$value"
}

resolve_download_base_url() {
  if [ -z "$OSS_ENDPOINT" ]; then
    printf '[install] OSS_ENDPOINT is required. Example: OSS_ENDPOINT=https://example.com scripts/install.sh latest\n' >&2
    exit 1
  fi

  local root="${OSS_ENDPOINT%/}"
  case "$root" in
    */"$PLUGIN_NAME")
      printf '%s' "$root"
      ;;
    *)
      printf '%s/%s' "$root" "$PLUGIN_NAME"
      ;;
  esac
}

download_archive() {
  local url="$1"
  local target="$2"
  log "downloading ${url}"
  curl -fL "$url" -o "$target"

  local checksum_path="${target}.sha256"
  if curl -fsSL "${url}.sha256" -o "$checksum_path"; then
    if command -v sha256sum >/dev/null 2>&1; then
      local expected
      local actual
      expected="$(sed -n '1s/[[:space:]].*//p' "$checksum_path")"
      actual="$(sha256sum "$target" | awk '{print $1}')"
      if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        printf '[install] sha256 verification failed: %s\n' "$url" >&2
        exit 1
      fi
      log "sha256 verified"
    else
      log "sha256sum is not available, skipping verification"
    fi
  else
    log "checksum not found, skipped sha256 verification"
  fi
}

download_latest_archive() {
  local target="$1"
  local base_url="${DOWNLOAD_BASE_URL%/}"
  download_archive "${base_url}/${PLUGIN_NAME}.tar.gz" "$target"
}

download_version_archive() {
  local version="$1"
  local target="$2"
  local base_url="${DOWNLOAD_BASE_URL%/}"
  download_archive "${base_url}/${PLUGIN_NAME}-v${version}.tar.gz" "$target"
}

extract_archive() {
  local archive_path="$1"
  local work_dir="$2"
  tar -xzf "$archive_path" -C "$work_dir"
  find "$work_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1
}

install_payload() {
  local payload_dir="$1"

  if [ ! -f "${payload_dir}/plugin.yaml" ] || [ ! -f "${payload_dir}/__init__.py" ] || [ ! -f "${payload_dir}/src/plugin.py" ]; then
    printf '[install] incomplete plugin archive contents: %s\n' "$payload_dir" >&2
    exit 1
  fi

  mkdir -p "$(dirname "$PLUGIN_DIR")"
  rm -rf "$PLUGIN_DIR"
  cp -R "$payload_dir" "$PLUGIN_DIR"
}

install_python_dependencies() {
  if [ ! -x "$HERMES_PYTHON" ]; then
    printf '[install] Hermes python was not found: %s\n' "$HERMES_PYTHON" >&2
    exit 1
  fi

  "$HERMES_PYTHON" - "$PLUGIN_DIR" <<'PY'
import pathlib
import subprocess
import sys
import tomllib

plugin_dir = pathlib.Path(sys.argv[1])
pyproject = plugin_dir / "pyproject.toml"
data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
deps = list(data.get("project", {}).get("dependencies", []) or [])
if deps:
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--break-system-packages",
            *deps,
        ]
    )
PY
  log "installed runtime dependencies into Hermes python"
}

configure_hermes_yaml() {
  require_command python3

  if ! python3 - <<'PY' >/dev/null 2>&1
import yaml
PY
  then
    printf '[install] python3 with PyYAML is required to update %s\n' "$CONFIG_FILE" >&2
    exit 1
  fi

  local tags_json='[]'
  if [ "${#TAGS[@]}" -gt 0 ]; then
    tags_json="$(printf '%s\n' "${TAGS[@]}" | python3 -c 'import json,sys; print(json.dumps([line.strip() for line in sys.stdin if line.strip()]))')"
  fi

  HERMES_CONFIG_FILE_RUNTIME="$CONFIG_FILE" \
  HERMES_PLUGIN_DIR_RUNTIME="$PLUGIN_DIR" \
  HERMES_PLUGIN_ENDPOINT_RUNTIME="$ENDPOINT" \
  HERMES_PLUGIN_INSTALL_TYPE_RUNTIME="$INSTALL_TYPE" \
  HERMES_PLUGIN_X_TOKEN_RUNTIME="$X_TOKEN" \
  HERMES_PLUGIN_SERVICE_NAME_RUNTIME="$SERVICE_NAME" \
  HERMES_PLUGIN_TAGS_RUNTIME="$tags_json" \
  python3 <<'PY'
import os
import pathlib
import sys

try:
    import yaml
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"[install] PyYAML is required in Hermes python: {exc}")

config_file = pathlib.Path(os.environ["HERMES_CONFIG_FILE_RUNTIME"])
plugin_dir = os.environ["HERMES_PLUGIN_DIR_RUNTIME"]
endpoint = os.environ.get("HERMES_PLUGIN_ENDPOINT_RUNTIME", "")
install_type = os.environ.get("HERMES_PLUGIN_INSTALL_TYPE_RUNTIME", "")
x_token = os.environ.get("HERMES_PLUGIN_X_TOKEN_RUNTIME", "")
service_name = os.environ.get("HERMES_PLUGIN_SERVICE_NAME_RUNTIME", "")
tags = yaml.safe_load(os.environ.get("HERMES_PLUGIN_TAGS_RUNTIME", "[]")) or []
plugin_id = "hermes-otel-plugin"

config = {}
if config_file.exists():
    raw = config_file.read_text(encoding="utf-8").strip()
    if raw:
      config = yaml.safe_load(raw) or {}

plugins = config.setdefault("plugins", {})
enabled = plugins.setdefault("enabled", [])
if plugin_id not in enabled:
    enabled.append(plugin_id)

section = config.setdefault("hermes_otel_plugin", {})
section["enabled"] = True
section.setdefault("resource_attributes", {})
section["resource_attributes"].setdefault("agent_runtime", "hermes")

if service_name:
    section["service_name"] = service_name

for tag in tags:
    key, sep, value = str(tag).partition("=")
    if not key or not sep:
        continue
    section["resource_attributes"][key] = value

if endpoint:
    section["endpoint"] = endpoint

if install_type == "gtrace":
    section["trace_path"] = "v1/write/otel-llm"
    section["metrics_path"] = "v1/write/otel-metrics"
    section["logs_enabled"] = False
    section["logs_path"] = "v1/write/otel-logs"
    headers = section.setdefault("headers", {})
    headers["to_headless"] = "true"
    if x_token:
        headers["X-Token"] = x_token

config_file.parent.mkdir(parents=True, exist_ok=True)
config_file.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
PY
}

restart_gateway_best_effort() {
  if ! command -v hermes >/dev/null 2>&1; then
    log "hermes command was not found, skipping gateway restart"
    return
  fi

  if hermes gateway restart >/dev/null 2>&1; then
    log "restarted Hermes gateway"
  else
    log "gateway restart failed or requires higher privileges; restart manually with: hermes gateway restart"
  fi
}

main() {
  require_command curl
  require_command tar

  INSTALL_TYPE="$(normalize_install_type "$INSTALL_TYPE")"
  if [ "$WRITE_CONFIG" -eq 1 ] && [ "$INSTALL_TYPE" = "gtrace" ]; then
    if [ -z "$ENDPOINT" ]; then
      printf '[install] type=gtrace requires --endpoint\n' >&2
      exit 1
    fi
    if [ -z "$X_TOKEN" ]; then
      printf '[install] type=gtrace requires --x-token\n' >&2
      exit 1
    fi
  fi

  tmp_dir="$(mktemp -d)"

  local archive_path="${tmp_dir}/plugin.tar.gz"
  local payload_dir
  local version

  case "$VERSION_INPUT" in
    http://*|https://*)
      log "downloading archive from custom url"
      download_archive "$VERSION_INPUT" "$archive_path"
      ;;
    *.tar.gz)
      log "using local archive ${VERSION_INPUT}"
      cp "$VERSION_INPUT" "$archive_path"
      ;;
    latest|"")
      DOWNLOAD_BASE_URL="$(resolve_download_base_url)"
      download_latest_archive "$archive_path"
      ;;
    *)
      DOWNLOAD_BASE_URL="$(resolve_download_base_url)"
      version="$(normalize_version "$VERSION_INPUT")"
      download_version_archive "$version" "$archive_path"
      ;;
  esac

  payload_dir="$(extract_archive "$archive_path" "$tmp_dir")"
  if [ -z "$payload_dir" ]; then
    printf '[install] no plugin directory found after extracting archive\n' >&2
    exit 1
  fi

  install_payload "$payload_dir"
  log "installed to ${PLUGIN_DIR}"

  if [ "$INSTALL_DEPS" -eq 1 ]; then
    install_python_dependencies
  else
    log "skipped dependency installation"
  fi

  if [ "$WRITE_CONFIG" -eq 1 ]; then
    configure_hermes_yaml
    log "updated ${CONFIG_FILE}"
  else
    cat <<EOF

Add this to ${CONFIG_FILE} manually:

plugins:
  enabled:
    - hermes-otel-plugin

hermes_otel_plugin:
  enabled: true
  endpoint: ${ENDPOINT:-http://127.0.0.1:9529/otel}
EOF
  fi

  if [ -n "$ENDPOINT" ]; then
    log "configured OTLP endpoint: ${ENDPOINT}"
  else
    log "endpoint is not set. Configure hermes_otel_plugin.endpoint in ${CONFIG_FILE} before use"
  fi
  log "install type: ${INSTALL_TYPE}"

  if [ "$RESTART_GATEWAY" -eq 1 ]; then
    restart_gateway_best_effort
  fi
}

main "$@"
