#!/usr/bin/env bash
#
# Idempotent installer for the Phase 3 SD card ingestion + upload services.
# Re-running it is safe: it converges on the same state and never overwrites an
# existing /etc/piuploader/config.env, queue, or ledger.
#
# No card needs to be inserted. Phase 3 detects and mounts any qualifying card
# at runtime (sdcard-mounter.service), so nothing card-specific is pinned here.
# Upgrading from Phase 2 removes the UUID-pinned udev rule and mount helper.
#
# Usage:  sudo ./setup.sh [options]
#   --skip-hardware-check     install on non-Pi hardware (development)
#   --reset-phase2-state      delete Phase 2 ledger/queue state, which Phase 3
#                             cannot read (see prd/phase-3-multi-card-ingestion.md §7.1)
#
set -euo pipefail

SERVICE_USER="piuploader"
INSTALL_DIR="/opt/piuploader"
CONFIG_DIR="/etc/piuploader"
CONFIG_FILE="${CONFIG_DIR}/config.env"
LOG_DIR="/var/log/piuploader"
STATE_DIR="/var/lib/piuploader"
REPORT_FILE="${LOG_DIR}/environment-report.txt"
HELPER_DIR="/usr/local/lib/piuploader"

# Phase 2 leftovers. Card mounting is a service now, not a udev rule.
LEGACY_MOUNT_HELPER="${HELPER_DIR}/mount-card.sh"
LEGACY_UDEV_RULE="/etc/udev/rules.d/99-piuploader-sdcard.rules"
LEGACY_UNIT="connectivity-daemon.service"

UNITS=("uploader.service" "sdcard-watcher.service" "sdcard-mounter.service")
PYTHON_MODULES=("uploader.py" "sdcard_watcher.py" "sdcard_mounter.py" "state.py")

# Filesystems the mounter will accept at runtime. Reported here so a missing
# kernel driver is visible at install time rather than at card insertion.
SUPPORTED_FILESYSTEMS=("vfat" "exfat" "ext4" "ext3" "ext2")

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKIP_HARDWARE_CHECK=0
RESET_PHASE2_STATE=0
FAILED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-hardware-check) SKIP_HARDWARE_CHECK=1 ;;
    --reset-phase2-state) RESET_PHASE2_STATE=1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '[setup] %s\n' "$*"; }
warn() { printf '[setup] WARNING: %s\n' "$*" >&2; }
fail() { printf '[setup] ERROR: %s\n' "$*" >&2; FAILED=1; }
die()  { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "setup.sh must run as root (use sudo)" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Environment detection
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
command -v lsblk >/dev/null 2>&1 || fail "lsblk is required by sdcard-mounter but is not installed"
command -v findmnt >/dev/null 2>&1 || fail "findmnt is required by sdcard-mounter but is not installed"
command -v mount >/dev/null 2>&1 || fail "mount is required by sdcard-mounter but is not installed"

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

# The uploader reads the WiFi association state; netdev grants access to the
# wireless tooling without granting root.
if getent group netdev >/dev/null 2>&1; then
  usermod -aG netdev "${SERVICE_USER}"
fi

install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${LOG_DIR}"
install -d -o root -g root -m 0755 "${INSTALL_DIR}"
# Queue and ledger. Created if absent, never emptied except by an explicit
# --reset-phase2-state: an existing queue holds files that have not reached the
# server yet.
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}/queue"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}/queue/pending"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}/queue/tmp"

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

config_has_key() {
  grep -Eq "^[[:space:]]*$1=" "${CONFIG_FILE}"
}

# Read the value the services will actually see. The subshell keeps API_KEY out
# of this script's environment, and no value is ever printed except the ones
# this script sets itself.
config_value() {
  (set -a; . "${CONFIG_FILE}" >/dev/null 2>&1; printf '%s' "${!1:-}")
}

ensure_config_key() {
  local key="$1" value="$2"
  if config_has_key "${key}"; then
    log "keeping existing ${key} in ${CONFIG_FILE}"
    return 0
  fi
  log "recording ${key}=${value} in ${CONFIG_FILE}"
  printf '\n# Recorded by setup.sh on %s\n%s=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${key}" "${value}" >> "${CONFIG_FILE}"
}

ensure_config_key "QUEUE_PATH" "${STATE_DIR}/queue"
ensure_config_key "STATE_DB_PATH" "${STATE_DIR}/state.db"
ensure_config_key "MAX_UPLOAD_BYTES" "20971520"
ensure_config_key "PING_INTERVAL_SECONDS" "300"
ensure_config_key "UPLOAD_TIMEOUT_SECONDS" "120"
ensure_config_key "CARD_MOUNTPOINT" "/mnt/sdcard"
ensure_config_key "CARD_MOUNT_INTERVAL_SECONDS" "2"

CARD_MOUNTPOINT="$(config_value CARD_MOUNTPOINT)"
[[ -n "${CARD_MOUNTPOINT}" ]] || die "CARD_MOUNTPOINT is empty in ${CONFIG_FILE}"
case "${CARD_MOUNTPOINT}" in
  /*) : ;;
  *) die "CARD_MOUNTPOINT must be an absolute path, got '${CARD_MOUNTPOINT}'" ;;
esac
install -d -o root -g root -m 0755 "${CARD_MOUNTPOINT}"

# Phase 2 pinned one card here. Phase 3 ignores both keys; say so once rather
# than editing a file the operator owns.
if config_has_key "CARD_UUID" || config_has_key "CARD_FILESYSTEM"; then
  log "note: CARD_UUID/CARD_FILESYSTEM in ${CONFIG_FILE} are ignored in Phase 3 and can be deleted"
fi

# ---------------------------------------------------------------------------
# 3. Phase 2 state guard (PRD §7.1)
# ---------------------------------------------------------------------------
# The ledger's primary key, the queue layout, and the server's uniqueness
# constraint all changed. Phase 2 state cannot be read under the new schema, so
# it is reset explicitly or setup stops.

STATE_DB="${STATE_DIR}/state.db"
SCHEMA_STATE="$("${PYTHON_BIN}" "${SCRIPT_DIR}/state.py" --schema-state "${STATE_DB}" 2>/dev/null || echo unreadable)"
FLAT_QUEUED="$(find "${STATE_DIR}/queue/pending" -mindepth 1 -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"

NEEDS_RESET=0
case "${SCHEMA_STATE}" in
  legacy)     NEEDS_RESET=1; log "found a Phase 2 ledger at ${STATE_DB}" ;;
  unreadable) NEEDS_RESET=1; warn "cannot read ${STATE_DB}; treating it as Phase 2 state" ;;
  absent|phase3) : ;;
  *)          NEEDS_RESET=1; warn "unexpected ledger state '${SCHEMA_STATE}'; treating it as Phase 2 state" ;;
esac
if [[ "${FLAT_QUEUED}" -gt 0 ]]; then
  NEEDS_RESET=1
  log "found ${FLAT_QUEUED} Phase 2 queue file(s) directly under ${STATE_DIR}/queue/pending"
fi

if [[ "${NEEDS_RESET}" -eq 1 && "${RESET_PHASE2_STATE}" -eq 0 ]]; then
  cat >&2 <<EOF
[setup] ERROR: Phase 2 local state is present and Phase 3 cannot read it.
[setup]   ledger: ${STATE_DB} (${SCHEMA_STATE})
[setup]   flat queue files under ${STATE_DIR}/queue/pending: ${FLAT_QUEUED}
[setup]
[setup] Phase 3 keys the ledger on (card_uuid, filename) and the queue on
[setup] pending/<card_uuid>/<filename>. Re-run with --reset-phase2-state to
[setup] delete the ledger and the queue contents and start clean:
[setup]
[setup]   sudo ./setup.sh --reset-phase2-state
[setup]
[setup] That permanently discards any file queued but not yet uploaded. The
[setup] cards are never cleared, so their files are re-ingested afterwards.
[setup] config.env, the API key, and logs are not touched.
EOF
  exit 1
fi

if [[ "${RESET_PHASE2_STATE}" -eq 1 ]]; then
  if [[ "${NEEDS_RESET}" -eq 0 ]]; then
    log "--reset-phase2-state: no Phase 2 state found, nothing to reset"
  else
    log "--reset-phase2-state: stopping services before touching local state"
    systemctl stop "${UNITS[@]}" >/dev/null 2>&1 || true

    log "deleting the Phase 2 ledger and queue contents"
    rm -f "${STATE_DB}" "${STATE_DB}-wal" "${STATE_DB}-shm"
    # -mindepth 1 keeps the directories themselves and covers dotfiles.
    find "${STATE_DIR}/queue/pending" -mindepth 1 -delete 2>/dev/null || true
    find "${STATE_DIR}/queue/tmp" -mindepth 1 -delete 2>/dev/null || true
    install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}/queue/pending"
    install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}/queue/tmp"
    log "reset complete; config.env, credentials, and logs were left alone"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Remove the Phase 2 UUID-pinned mount (PRD §5.1)
# ---------------------------------------------------------------------------
# Mounting is sdcard-mounter.service's job now. Leaving the old rule in place
# would race it for the same mountpoint.

if [[ -f "${LEGACY_UDEV_RULE}" ]]; then
  log "removing the Phase 2 udev mount rule ${LEGACY_UDEV_RULE}"
  rm -f "${LEGACY_UDEV_RULE}"
  if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules
    log "reloaded udev rules"
  else
    warn "udevadm not found; reboot to drop the old card rule"
  fi
fi
if [[ -f "${LEGACY_MOUNT_HELPER}" ]]; then
  log "removing the Phase 2 mount helper ${LEGACY_MOUNT_HELPER}"
  rm -f "${LEGACY_MOUNT_HELPER}"
fi

# Report which supported filesystems this kernel can actually mount, so a
# missing driver shows up now instead of when a card is inserted.
filesystem_is_mountable() {
  local fs="$1"
  grep -qw "${fs}" /proc/filesystems && return 0
  modprobe -q "${fs}" 2>/dev/null && grep -qw "${fs}" /proc/filesystems && return 0
  [[ -x "/sbin/mount.${fs}" || -x "/usr/sbin/mount.${fs}" ]] && return 0
  return 1
}

MOUNTABLE_FILESYSTEMS=()
UNMOUNTABLE_FILESYSTEMS=()
for fs in "${SUPPORTED_FILESYSTEMS[@]}"; do
  if filesystem_is_mountable "${fs}"; then
    MOUNTABLE_FILESYSTEMS+=("${fs}")
  else
    UNMOUNTABLE_FILESYSTEMS+=("${fs}")
  fi
done
log "card filesystems this kernel can mount: ${MOUNTABLE_FILESYSTEMS[*]:-none}"
if [[ "${#UNMOUNTABLE_FILESYSTEMS[@]}" -gt 0 ]]; then
  warn "a card formatted ${UNMOUNTABLE_FILESYSTEMS[*]} cannot be mounted here (for exfat: 'sudo apt-get install exfatprogs')"
fi

# ---------------------------------------------------------------------------
# 5. Optional pyudev fast path
# ---------------------------------------------------------------------------
# Without pyudev the watcher polls on CARD_SCAN_INTERVAL_SECONDS instead of
# reacting to the udev event. That costs latency, not correctness. The mounter
# polls regardless.

if "${PYTHON_BIN}" -c 'import pyudev' >/dev/null 2>&1; then
  log "pyudev is available (card insertions are noticed immediately)"
elif command -v apt-get >/dev/null 2>&1; then
  log "installing python3-pyudev"
  if ! apt-get install -y python3-pyudev >/dev/null 2>&1; then
    warn "could not install python3-pyudev; the watcher will poll for the card instead"
  fi
else
  warn "pyudev is not installed and apt-get is unavailable; the watcher will poll for the card instead"
fi

# ---------------------------------------------------------------------------
# 6. Services
# ---------------------------------------------------------------------------

for module in "${PYTHON_MODULES[@]}"; do
  install -o root -g root -m 0755 "${SCRIPT_DIR}/${module}" "${INSTALL_DIR}/${module}"
done

for unit in "${UNITS[@]}"; do
  install -o root -g root -m 0644 "${SCRIPT_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"
done

# Phase 1's daemon is superseded by uploader.service. The config, queue, and
# ledger are untouched.
if [[ -f "/etc/systemd/system/${LEGACY_UNIT}" ]]; then
  log "removing the Phase 1 ${LEGACY_UNIT} (replaced by uploader.service)"
  systemctl disable --now "${LEGACY_UNIT}" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/${LEGACY_UNIT}"
fi
rm -f "${INSTALL_DIR}/connectivity_daemon.py"

log "reloading systemd and enabling ${UNITS[*]}"
systemctl daemon-reload
systemctl enable "${UNITS[@]}"

# ---------------------------------------------------------------------------
# 7. Configuration cross-check and report
# ---------------------------------------------------------------------------

CONFIGURED_WIFI="$(config_value WIFI_INTERFACE)"
CONFIGURED_WIFI="${CONFIGURED_WIFI:-wlan0}"
if [[ "${CONFIGURED_WIFI}" != "${DETECTED_WIFI}" ]]; then
  warn "WIFI_INTERFACE is '${CONFIGURED_WIFI}' but the detected adapter is '${DETECTED_WIFI}' — update ${CONFIG_FILE}"
fi

if grep -q '^API_KEY=replace-me$' "${CONFIG_FILE}"; then
  warn "${CONFIG_FILE} still has the placeholder API_KEY; both services will get 401s until you set the real key"
fi
if grep -q '^SERVER_URL=https://your-service.up.railway.app$' "${CONFIG_FILE}"; then
  warn "${CONFIG_FILE} still has the placeholder SERVER_URL"
fi

QUEUED_COUNT="$(find "${STATE_DIR}/queue/pending" -mindepth 2 -maxdepth 2 -type f 2>/dev/null | wc -l | tr -d ' ')"
QUEUED_CARDS="$(find "${STATE_DIR}/queue/pending" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"

{
  echo "Phase 3 environment report — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Pi model:             ${PI_MODEL}"
  echo "OS:                   ${OS_NAME}"
  echo "OS version:           ${OS_VERSION}"
  echo "Architecture:         ${ARCH}"
  echo "Kernel:               ${KERNEL}"
  echo "Python 3:             ${PYTHON_VERSION}"
  echo "systemd:              ${SYSTEMD_VERSION}"
  echo "Detected WiFi:        ${DETECTED_WIFI}"
  echo "Configured WiFi:      ${CONFIGURED_WIFI}"
  echo "Hostname / device_id: ${HOSTNAME_SHORT}"
  echo "Card mountpoint:      ${CARD_MOUNTPOINT}"
  echo "Mount interval:       $(config_value CARD_MOUNT_INTERVAL_SECONDS)s"
  echo "Mountable card FS:    ${MOUNTABLE_FILESYSTEMS[*]:-none}"
  echo "Queue:                ${STATE_DIR}/queue (${QUEUED_COUNT} file(s) across ${QUEUED_CARDS} card(s))"
  echo "Ledger:               ${STATE_DIR}/state.db"
  echo "pyudev:               $("${PYTHON_BIN}" -c 'import pyudev; print(pyudev.__version__)' 2>/dev/null || echo 'not installed (polling fallback)')"
} | tee "${REPORT_FILE}"
chown "${SERVICE_USER}":"${SERVICE_USER}" "${REPORT_FILE}"

log "wrote environment report to ${REPORT_FILE} — copy these values into README.md"
log "next: edit ${CONFIG_FILE}, then run 'sudo systemctl restart ${UNITS[*]}'"
log "follow logs with: journalctl -u sdcard-mounter -u sdcard-watcher -u uploader -f"
