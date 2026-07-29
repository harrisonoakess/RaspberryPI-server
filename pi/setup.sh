#!/usr/bin/env bash
#
# Idempotent installer for the Phase 1 connectivity daemon.
# Re-running it is safe: it converges on the same state and never overwrites
# an existing /etc/piuploader/config.env.
#
# Usage:  sudo ./setup.sh [--skip-hardware-check]
#
set -euo pipefail

SERVICE_USER="piuploader"
INSTALL_DIR="/opt/piuploader"
CONFIG_DIR="/etc/piuploader"
CONFIG_FILE="${CONFIG_DIR}/config.env"
LOG_DIR="/var/log/piuploader"
REPORT_FILE="${LOG_DIR}/environment-report.txt"
UNIT_NAME="connectivity-daemon.service"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKIP_HARDWARE_CHECK=0
FAILED=0

for arg in "$@"; do
  case "$arg" in
    --skip-hardware-check) SKIP_HARDWARE_CHECK=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '[setup] %s\n' "$*"; }
warn() { printf '[setup] WARNING: %s\n' "$*" >&2; }
fail() { printf '[setup] ERROR: %s\n' "$*" >&2; FAILED=1; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "setup.sh must run as root (use sudo)" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Environment detection (Success Criteria 1)
# ---------------------------------------------------------------------------

PI_MODEL="unknown"
if [[ -r /proc/device-tree/model ]]; then
  PI_MODEL="$(tr -d '\0' < /proc/device-tree/model)"
fi

OS_NAME="unknown"; OS_VERSION="unknown"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_NAME="${PRETTY_NAME:-${NAME:-unknown}}"
  OS_VERSION="${VERSION:-${VERSION_ID:-unknown}}"
fi

ARCH="$(uname -m)"
KERNEL="$(uname -r)"
PYTHON_BIN="$(command -v python3 || true)"
PYTHON_VERSION="missing"
[[ -n "${PYTHON_BIN}" ]] && PYTHON_VERSION="$("${PYTHON_BIN}" --version 2>&1)"

SYSTEMD_VERSION="missing"
command -v systemctl >/dev/null 2>&1 && SYSTEMD_VERSION="$(systemctl --version | head -n1)"

# Detected built-in wireless adapter. `iw dev` is authoritative; fall back to sysfs.
DETECTED_WIFI=""
if command -v iw >/dev/null 2>&1; then
  DETECTED_WIFI="$(iw dev 2>/dev/null | awk '/Interface/ {print $2; exit}')"
fi
if [[ -z "${DETECTED_WIFI}" ]]; then
  for candidate in /sys/class/net/*/wireless; do
    [[ -e "${candidate}" ]] || continue
    DETECTED_WIFI="$(basename "$(dirname "${candidate}")")"
    break
  done
fi
[[ -z "${DETECTED_WIFI}" ]] && DETECTED_WIFI="none-detected"

log "Pi model:        ${PI_MODEL}"
log "OS:              ${OS_NAME} (version ${OS_VERSION})"
log "Architecture:    ${ARCH}"
log "Kernel:          ${KERNEL}"
log "Python 3:        ${PYTHON_VERSION} (${PYTHON_BIN:-not found})"
log "systemd:         ${SYSTEMD_VERSION}"
log "WiFi interface:  ${DETECTED_WIFI}"
log "Hostname:        $(hostname)"

if [[ "${SKIP_HARDWARE_CHECK}" -eq 0 ]]; then
  case "${PI_MODEL}" in
    *"Raspberry Pi 3 Model B Plus"*|*"Raspberry Pi 3 Model B+"*) : ;;
    *) fail "expected a Raspberry Pi 3 Model B+, detected '${PI_MODEL}' (pass --skip-hardware-check to override)" ;;
  esac
  case "${ARCH}" in
    aarch64|arm64) : ;;
    *) fail "expected Raspberry Pi OS 64-bit, detected architecture '${ARCH}' (pass --skip-hardware-check to override)" ;;
  esac
fi

[[ -z "${PYTHON_BIN}" ]] && fail "python3 is required but not installed"
[[ "${SYSTEMD_VERSION}" == "missing" ]] && fail "systemd (systemctl) is required but not available"

# device_id is derived from the hostname at runtime; it must satisfy the
# server's validation rules (1-63 chars, alphanumeric ends, internal hyphens).
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname)"
if ! [[ "${HOSTNAME_SHORT}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]]; then
  fail "hostname '${HOSTNAME_SHORT}' is not a valid device_id; set one with 'sudo hostnamectl set-hostname <name>'"
fi

if [[ "${FAILED}" -ne 0 ]]; then
  echo "[setup] aborting: fix the errors above and re-run" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Service account, directories, and configuration
# ---------------------------------------------------------------------------

if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  log "service user ${SERVICE_USER} already exists"
else
  log "creating system user ${SERVICE_USER}"
  useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

# The daemon reads the WiFi association state; netdev grants access to the
# wireless tooling without granting root.
if getent group netdev >/dev/null 2>&1; then
  usermod -aG netdev "${SERVICE_USER}"
fi

install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${LOG_DIR}"
install -d -o root -g root -m 0755 "${INSTALL_DIR}"

if [[ -f "${CONFIG_FILE}" ]]; then
  log "keeping existing ${CONFIG_FILE}"
else
  log "installing ${CONFIG_FILE} from config.env.example — edit SERVER_URL and API_KEY"
  install -o root -g "${SERVICE_USER}" -m 0640 "${SCRIPT_DIR}/config.env.example" "${CONFIG_FILE}"
fi
# Enforce ownership/permissions even on a pre-existing file: the API key must
# never be world-readable.
chown root:"${SERVICE_USER}" "${CONFIG_FILE}"
chmod 0640 "${CONFIG_FILE}"

# ---------------------------------------------------------------------------
# 3. Daemon and systemd unit
# ---------------------------------------------------------------------------

install -o root -g root -m 0755 "${SCRIPT_DIR}/connectivity_daemon.py" "${INSTALL_DIR}/connectivity_daemon.py"
install -o root -g root -m 0644 "${SCRIPT_DIR}/systemd/${UNIT_NAME}" "${UNIT_DEST}"

log "reloading systemd and enabling ${UNIT_NAME}"
systemctl daemon-reload
systemctl enable "${UNIT_NAME}"

# ---------------------------------------------------------------------------
# 4. Configuration cross-check and report
# ---------------------------------------------------------------------------

# Read the value the daemon will actually see. The subshell keeps API_KEY out of
# this script's environment, and it is never printed.
CONFIGURED_WIFI="$(set -a; . "${CONFIG_FILE}"; printf '%s' "${WIFI_INTERFACE:-wlan0}")"
if [[ "${CONFIGURED_WIFI}" != "${DETECTED_WIFI}" ]]; then
  warn "WIFI_INTERFACE is '${CONFIGURED_WIFI}' but the detected adapter is '${DETECTED_WIFI}' — update ${CONFIG_FILE}"
fi

if grep -q '^API_KEY=replace-me$' "${CONFIG_FILE}"; then
  warn "${CONFIG_FILE} still has the placeholder API_KEY; the daemon will get 401s until you set the real key"
fi

{
  echo "Phase 1 environment report — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Pi model:            ${PI_MODEL}"
  echo "OS:                  ${OS_NAME}"
  echo "OS version:          ${OS_VERSION}"
  echo "Architecture:        ${ARCH}"
  echo "Kernel:              ${KERNEL}"
  echo "Python 3:            ${PYTHON_VERSION}"
  echo "systemd:             ${SYSTEMD_VERSION}"
  echo "Detected WiFi:       ${DETECTED_WIFI}"
  echo "Configured WiFi:     ${CONFIGURED_WIFI}"
  echo "Hostname / device_id: ${HOSTNAME_SHORT}"
} | tee "${REPORT_FILE}"
chown "${SERVICE_USER}":"${SERVICE_USER}" "${REPORT_FILE}"

log "wrote environment report to ${REPORT_FILE} — copy these values into README.md"
log "next: edit ${CONFIG_FILE}, then run 'sudo systemctl restart ${UNIT_NAME}'"
log "follow logs with: journalctl -u ${UNIT_NAME} -f"
