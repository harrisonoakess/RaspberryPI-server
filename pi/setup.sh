#!/usr/bin/env bash
#
# Idempotent installer for the Phase 2 SD card ingestion + upload services.
# Re-running it is safe: it converges on the same state and never overwrites an
# existing /etc/piuploader/config.env, queue, or ledger. Upgrading from Phase 1
# replaces connectivity-daemon.service with uploader.service and
# sdcard-watcher.service.
#
# Usage:  sudo ./setup.sh [options]
#   --skip-hardware-check     install on non-Pi hardware (development)
#   --card-device /dev/sda1   name the card partition instead of auto-detecting
#   --redetect-card           overwrite a CARD_UUID already in config.env
#   --skip-card-detection     install the services without touching the card
#                             config (for re-runs with no card inserted)
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
MOUNT_HELPER="${HELPER_DIR}/mount-card.sh"
UDEV_RULE="/etc/udev/rules.d/99-piuploader-sdcard.rules"

LEGACY_UNIT="connectivity-daemon.service"
UNITS=("uploader.service" "sdcard-watcher.service")
PYTHON_MODULES=("uploader.py" "sdcard_watcher.py" "state.py")

# Filesystems this installer will mount. Anything else fails setup rather than
# being mounted with guessed options.
SUPPORTED_FILESYSTEMS=("vfat" "exfat" "ext4" "ext3" "ext2")

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKIP_HARDWARE_CHECK=0
SKIP_CARD_DETECTION=0
REDETECT_CARD=0
CARD_DEVICE=""
FAILED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-hardware-check) SKIP_HARDWARE_CHECK=1 ;;
    --skip-card-detection) SKIP_CARD_DETECTION=1 ;;
    --redetect-card) REDETECT_CARD=1 ;;
    --card-device)
      shift
      [[ $# -gt 0 ]] || { echo "--card-device needs a value" >&2; exit 2; }
      CARD_DEVICE="$1"
      ;;
    --card-device=*) CARD_DEVICE="${1#*=}" ;;
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

SYSTEMD_MOUNT="$(command -v systemd-mount || true)"
SYSTEMD_UMOUNT="$(command -v systemd-umount || true)"
MOUNTPOINT_BIN="$(command -v mountpoint || true)"

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

# The uploader reads the WiFi association state; netdev grants access to the
# wireless tooling without granting root.
if getent group netdev >/dev/null 2>&1; then
  usermod -aG netdev "${SERVICE_USER}"
fi

install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${LOG_DIR}"
install -d -o root -g root -m 0755 "${INSTALL_DIR}"
install -d -o root -g root -m 0755 "${HELPER_DIR}"
# Queue and ledger. Created if absent, never emptied: an existing queue holds
# files that have not reached the server yet.
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

replace_config_key() {
  local key="$1" value="$2"
  if config_has_key "${key}"; then
    log "updating ${key}=${value} in ${CONFIG_FILE}"
    sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${value}|" "${CONFIG_FILE}"
  else
    ensure_config_key "${key}" "${value}"
  fi
}

ensure_config_key "QUEUE_PATH" "${STATE_DIR}/queue"
ensure_config_key "STATE_DB_PATH" "${STATE_DIR}/state.db"
ensure_config_key "MAX_UPLOAD_BYTES" "10485760"
ensure_config_key "PING_INTERVAL_SECONDS" "300"
ensure_config_key "UPLOAD_TIMEOUT_SECONDS" "120"
ensure_config_key "CARD_MOUNTPOINT" "/mnt/sdcard"

CARD_MOUNTPOINT="$(config_value CARD_MOUNTPOINT)"
[[ -n "${CARD_MOUNTPOINT}" ]] || die "CARD_MOUNTPOINT is empty in ${CONFIG_FILE}"
case "${CARD_MOUNTPOINT}" in
  /*) : ;;
  *) die "CARD_MOUNTPOINT must be an absolute path, got '${CARD_MOUNTPOINT}'" ;;
esac
install -d -o root -g root -m 0755 "${CARD_MOUNTPOINT}"

# ---------------------------------------------------------------------------
# 3. SD card detection (PRD §4, §6)
# ---------------------------------------------------------------------------

CARD_UUID="$(config_value CARD_UUID)"
CARD_FILESYSTEM="$(config_value CARD_FILESYSTEM)"

filesystem_is_supported() {
  local fs="$1" supported
  for supported in "${SUPPORTED_FILESYSTEMS[@]}"; do
    [[ "${fs}" == "${supported}" ]] || continue
    # Supported by this installer; now check the kernel can actually mount it.
    grep -qw "${fs}" /proc/filesystems && return 0
    modprobe -q "${fs}" 2>/dev/null && grep -qw "${fs}" /proc/filesystems && return 0
    [[ -x "/sbin/mount.${fs}" || -x "/usr/sbin/mount.${fs}" ]] && return 0
    return 2   # recognised but unmountable here
  done
  return 1     # not supported by this installer
}

# Pull one KEY="value" pair out of an `lsblk -P` line. Parsed with a bash regex
# rather than eval, so no device or filesystem name is ever executed as shell.
lsblk_field() {
  local key="$1" line="$2"
  if [[ "${line}" =~ (^|[[:space:]])${key}=\"([^\"]*)\" ]]; then
    printf '%s' "${BASH_REMATCH[2]}"
  fi
}

# Removable partitions that carry a filesystem, excluding the disk that holds /.
list_card_candidates() {
  local root_source root_disk
  root_source="$(findmnt -no SOURCE / 2>/dev/null || true)"
  root_disk=""
  [[ -n "${root_source}" ]] && root_disk="$(lsblk -no PKNAME "${root_source}" 2>/dev/null | head -n1 || true)"

  local line device type removable hotplug fstype uuid parent
  while IFS= read -r line; do
    type="$(lsblk_field TYPE "${line}")"
    [[ "${type}" == "part" ]] || continue
    # Some USB card readers report RM=0 and only set HOTPLUG=1.
    removable="$(lsblk_field RM "${line}")"
    hotplug="$(lsblk_field HOTPLUG "${line}")"
    [[ "${removable}" == "1" || "${hotplug}" == "1" ]] || continue
    fstype="$(lsblk_field FSTYPE "${line}")"
    uuid="$(lsblk_field UUID "${line}")"
    [[ -n "${uuid}" && -n "${fstype}" ]] || continue
    parent="$(lsblk_field PKNAME "${line}")"
    [[ -n "${root_disk}" && "${parent}" == "${root_disk}" ]] && continue
    device="$(lsblk_field PATH "${line}")"
    printf '%s\t%s\t%s\n' "${device}" "${fstype}" "${uuid}"
  done < <(lsblk -P -o PATH,TYPE,RM,HOTPLUG,FSTYPE,UUID,PKNAME)
}

if [[ "${SKIP_CARD_DETECTION}" -eq 1 ]]; then
  log "skipping card detection (--skip-card-detection)"
elif [[ -n "${CARD_UUID}" && "${REDETECT_CARD}" -eq 0 ]]; then
  log "card already configured: UUID=${CARD_UUID} filesystem=${CARD_FILESYSTEM:-unrecorded}"
  log "pass --redetect-card to replace it with a different card"
else
  command -v lsblk >/dev/null 2>&1 || die "lsblk is required to detect the card"

  DETECTED_FS=""
  DETECTED_UUID=""

  if [[ -n "${CARD_DEVICE}" ]]; then
    [[ -b "${CARD_DEVICE}" ]] || die "${CARD_DEVICE} is not a block device"
    DETECTED_FS="$(lsblk -no FSTYPE "${CARD_DEVICE}" | head -n1 | tr -d '[:space:]')"
    DETECTED_UUID="$(lsblk -no UUID "${CARD_DEVICE}" | head -n1 | tr -d '[:space:]')"
    if [[ -z "${DETECTED_UUID}" ]] && command -v blkid >/dev/null 2>&1; then
      DETECTED_UUID="$(blkid -s UUID -o value "${CARD_DEVICE}" || true)"
      DETECTED_FS="${DETECTED_FS:-$(blkid -s TYPE -o value "${CARD_DEVICE}" || true)}"
    fi
    [[ -n "${DETECTED_UUID}" ]] || die "${CARD_DEVICE} has no filesystem UUID; format the card first"
    log "using the card given on the command line: ${CARD_DEVICE}"
  else
    mapfile -t CANDIDATES < <(list_card_candidates)
    if [[ "${#CANDIDATES[@]}" -eq 0 ]]; then
      echo "[setup] ERROR: no removable partition with a filesystem was found." >&2
      echo "[setup] Insert the logger's SD card, then re-run. Current block devices:" >&2
      lsblk --fs >&2 || true
      exit 1
    fi
    if [[ "${#CANDIDATES[@]}" -gt 1 ]]; then
      echo "[setup] ERROR: more than one removable partition was found; name the card explicitly." >&2
      printf '[setup]   %s\n' "${CANDIDATES[@]}" >&2
      echo "[setup] Re-run with: sudo ./setup.sh --card-device /dev/sdX1" >&2
      exit 1
    fi
    IFS=$'\t' read -r CARD_DEVICE DETECTED_FS DETECTED_UUID <<< "${CANDIDATES[0]}"
    log "detected one removable card: ${CARD_DEVICE}"
  fi

  log "card filesystem: ${DETECTED_FS:-unknown}"
  log "card UUID:       ${DETECTED_UUID}"

  set +e
  filesystem_is_supported "${DETECTED_FS}"
  FS_STATUS=$?
  set -e
  case "${FS_STATUS}" in
    0) : ;;
    2) die "filesystem '${DETECTED_FS}' is recognised but this kernel cannot mount it; install its driver (for exfat: 'sudo apt-get install exfatprogs') and re-run" ;;
    *) die "filesystem '${DETECTED_FS:-unknown}' on ${CARD_DEVICE} is not supported; supported: ${SUPPORTED_FILESYSTEMS[*]}" ;;
  esac

  replace_config_key "CARD_UUID" "${DETECTED_UUID}"
  replace_config_key "CARD_FILESYSTEM" "${DETECTED_FS}"
  CARD_UUID="${DETECTED_UUID}"
  CARD_FILESYSTEM="${DETECTED_FS}"
fi

# ---------------------------------------------------------------------------
# 4. Root-owned read-only mount (udev rule + helper)
# ---------------------------------------------------------------------------
# The services are unprivileged, so root provisions the mount: udev sees the
# configured UUID appear and asks PID 1 to mount it read-only with
# nodev,nosuid,noexec. The watcher only ever reads that mountpoint.

if [[ -z "${CARD_UUID}" ]]; then
  warn "no CARD_UUID configured, so no mount rule was installed; the watcher will not start until you re-run setup.sh with the card inserted"
elif [[ -z "${SYSTEMD_MOUNT}" ]]; then
  warn "systemd-mount was not found, so no mount rule was installed; mount ${CARD_MOUNTPOINT} read-only by another means"
else
  install -d -o root -g root -m 0755 "${HELPER_DIR}"
  cat > "${MOUNT_HELPER}" <<EOF
#!/usr/bin/env bash
# Installed by pi/setup.sh — do not edit; re-run setup.sh instead.
# Called by udev when the configured card appears. Mounts it read-only for the
# unprivileged piuploader services. systemd-mount hands the work to PID 1, so
# the mount outlives this short-lived udev worker.
set -u
DEVNODE="\${1:-}"
[[ -n "\${DEVNODE}" ]] || exit 1

# A hot-unplug can leave the previous mount behind; clear it before remounting.
if ${MOUNTPOINT_BIN:-/usr/bin/mountpoint} -q "${CARD_MOUNTPOINT}"; then
  ${SYSTEMD_UMOUNT:-/usr/bin/systemd-umount} "${CARD_MOUNTPOINT}" 2>/dev/null || \
    umount -l "${CARD_MOUNTPOINT}" 2>/dev/null || true
fi

exec ${SYSTEMD_MOUNT} --no-block --collect \\
  --type="${CARD_FILESYSTEM}" \\
  --options=ro,nodev,nosuid,noexec \\
  "\${DEVNODE}" "${CARD_MOUNTPOINT}"
EOF
  chown root:root "${MOUNT_HELPER}"
  chmod 0755 "${MOUNT_HELPER}"

  cat > "${UDEV_RULE}" <<EOF
# Installed by pi/setup.sh — do not edit; re-run setup.sh instead.
# Exactly one card is accepted, matched by filesystem UUID. No other removable
# drive is ever mounted at ${CARD_MOUNTPOINT}.
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_UUID}=="${CARD_UUID}", RUN+="${MOUNT_HELPER} \$devnode"
ACTION=="remove", SUBSYSTEM=="block", ENV{ID_FS_UUID}=="${CARD_UUID}", RUN+="${SYSTEMD_UMOUNT:-/usr/bin/systemd-umount} ${CARD_MOUNTPOINT}"
EOF
  chown root:root "${UDEV_RULE}"
  chmod 0644 "${UDEV_RULE}"
  log "installed ${UDEV_RULE} for UUID ${CARD_UUID} (${CARD_FILESYSTEM})"

  if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules
    log "reloaded udev rules"
  else
    warn "udevadm not found; reboot for the card rule to take effect"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Optional pyudev fast path
# ---------------------------------------------------------------------------
# Without pyudev the watcher polls for the card on CARD_SCAN_INTERVAL_SECONDS
# instead of reacting to the udev event. That costs latency, not correctness.

if "${PYTHON_BIN}" -c 'import pyudev' >/dev/null 2>&1; then
  log "pyudev is available (card insertions are detected immediately)"
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
if [[ -z "$(config_value CARD_UUID)" ]]; then
  warn "CARD_UUID is not set; sdcard-watcher will exit with a configuration error until you re-run setup.sh with the card inserted"
fi

QUEUED_COUNT="$(find "${STATE_DIR}/queue/pending" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"

{
  echo "Phase 2 environment report — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
  echo "Card UUID:            $(config_value CARD_UUID)"
  echo "Card filesystem:      $(config_value CARD_FILESYSTEM)"
  echo "Card mountpoint:      ${CARD_MOUNTPOINT}"
  echo "Card device at setup: ${CARD_DEVICE:-not detected this run}"
  echo "Queue:                ${STATE_DIR}/queue (${QUEUED_COUNT} file(s) pending)"
  echo "Ledger:               ${STATE_DIR}/state.db"
  echo "pyudev:               $("${PYTHON_BIN}" -c 'import pyudev; print(pyudev.__version__)' 2>/dev/null || echo 'not installed (polling fallback)')"
} | tee "${REPORT_FILE}"
chown "${SERVICE_USER}":"${SERVICE_USER}" "${REPORT_FILE}"

log "wrote environment report to ${REPORT_FILE} — copy these values into README.md"
log "next: edit ${CONFIG_FILE}, then run 'sudo systemctl restart ${UNITS[*]}'"
log "follow logs with: journalctl -u uploader -u sdcard-watcher -f"
