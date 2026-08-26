#!/bin/bash
# Run on a cloned runner VM as root (qemu-guest-agent exec).
# Environment is sourced from /tmp/proxmox-runner-fleet.env (written by the
# fleet tool). Do not echo tokens.
set -euo pipefail

ENV_FILE="${FLEET_ENV_FILE:-/tmp/proxmox-runner-fleet.env}"
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${RUNNER_URL:?}"
: "${RUNNER_TOKEN:?}"
: "${RUNNER_NAME:?}"
: "${RUNNER_LABELS:?}"
: "${RUNNER_USER:?}"
: "${RUNNER_DIR:?}"
: "${RUNNER_TARBALL_URL:?}"

umask 077

write_bundled_disk_cleanup() {
    # Fallback when fleet deploy did not upload tools/proxmox-runners/disk-cleanup.
    # Bodies must stay identical to that directory (enforced by unit tests).
    mkdir -p "$1"
    cat >"$1/actions-runner-disk-cleanup" <<'ACTIONS_RUNNER_DISK_CLEANUP_SCRIPT'
#!/bin/bash
# Idle disk cleanup for self-hosted GitHub Actions runners.
# Never interrupt a running job (Runner.Worker). Always uncordon the runner
# service, even when a cleanup step fails.
set -euo pipefail

THRESHOLD="${DISK_CLEANUP_THRESHOLD:-70}"
RUNNER_DIR="${RUNNER_DIR:-/opt/actions-runner}"
RUNNER_USER="${RUNNER_USER:-runner}"
DOCKER_DATA_ROOT="${DOCKER_DATA_ROOT:-/var/lib/docker}"
LOCK_FILE="${LOCK_FILE:-/run/actions-runner-disk-cleanup.lock}"
IF_PRESSURE=0

log() {
    echo "actions-runner-disk-cleanup: $*"
}

usage() {
    cat <<'EOF'
Usage: actions-runner-disk-cleanup [--if-pressure]

  (default)       Clean when the runner is idle (6h timer).
  --if-pressure   Clean only when idle AND disk use is >= 70% on the
                  filesystems that hold the runner directory or Docker data.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --if-pressure) IF_PRESSURE=1 ;;
        --help|-h)
            usage
            exit 0
            ;;
        --threshold=*)
            THRESHOLD="${arg#--threshold=}"
            ;;
        *)
            log "unknown argument: $arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

job_running() {
    # Runner.Worker is the Actions job process. Runner.Listener stays up while
    # idle and must not be treated as a running job.
    if command -v pgrep >/dev/null 2>&1; then
        if pgrep -x Runner.Worker >/dev/null 2>&1; then
            return 0
        fi
        if pgrep -f '[R]unner.Worker' >/dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

usage_pct_for_path() {
    local path="$1"
    while [ -n "$path" ] && [ "$path" != "/" ] && [ ! -e "$path" ]; do
        path=$(dirname "$path")
    done
    [ -e "$path" ] || path=/
    df -P "$path" 2>/dev/null | awk 'NR==2 { gsub(/%/, "", $5); if ($5 ~ /^[0-9]+$/) print $5 }'
}

docker_data_paths() {
    local root=""
    if command -v docker >/dev/null 2>&1; then
        root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
    fi
    if [ -n "$root" ]; then
        printf '%s\n' "$root"
    elif [ -e "$DOCKER_DATA_ROOT" ]; then
        printf '%s\n' "$DOCKER_DATA_ROOT"
    fi
}

disk_over_threshold() {
    local path pct
    local -a paths=("$RUNNER_DIR")
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        paths+=("$path")
    done < <(docker_data_paths)

    for path in "${paths[@]}"; do
        pct=$(usage_pct_for_path "$path" || true)
        [ -n "${pct:-}" ] || continue
        if [ "$pct" -ge "$THRESHOLD" ]; then
            log "filesystem for ${path} is ${pct}% full (>= ${THRESHOLD}%)"
            return 0
        fi
        log "filesystem for ${path} is ${pct}% full (below ${THRESHOLD}%)"
    done
    return 1
}

list_runner_units() {
    command -v systemctl >/dev/null 2>&1 || return 0
    systemctl list-units --type=service --all --no-legend --no-pager --plain 'actions.runner.*' 2>/dev/null \
        | awk '{print $1}' \
        | grep -E '^actions\.runner\..+\.service$' || true
}

CORDONED_UNITS=()
uncordon() {
    local unit
    for unit in "${CORDONED_UNITS[@]+"${CORDONED_UNITS[@]}"}"; do
        log "starting ${unit}"
        systemctl start "$unit" || log "warning: failed to start ${unit}"
    done
}

install -d -m 0755 "$(dirname "$LOCK_FILE")" 2>/dev/null || true
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another cleanup is already running; skip"
    exit 0
fi

if job_running; then
    log "job running (Runner.Worker); skip"
    exit 0
fi

if [ "$IF_PRESSURE" -eq 1 ]; then
    if ! disk_over_threshold; then
        log "disk below ${THRESHOLD}% on runner/Docker filesystems; skip"
        exit 0
    fi
    if job_running; then
        log "job running after disk check; skip"
        exit 0
    fi
fi

if job_running; then
    log "job running on re-check; skip"
    exit 0
fi

trap uncordon EXIT

mapfile -t CORDONED_UNITS < <(list_runner_units)
if [ "${#CORDONED_UNITS[@]}" -gt 0 ]; then
    log "idle confirmed; cordoning ${CORDONED_UNITS[*]}"
    if job_running; then
        log "job running immediately before cordon; skip"
        CORDONED_UNITS=()
        exit 0
    fi
    local_unit=""
    for local_unit in "${CORDONED_UNITS[@]}"; do
        systemctl stop "$local_unit" || log "warning: failed to stop ${local_unit}"
    done
else
    log "no actions.runner.*.service units loaded; continuing without cordon"
fi

if job_running; then
    log "job still present after cordon; skip cleanup (will uncordon)"
    exit 0
fi

cleanup_diag() {
    local diag="${RUNNER_DIR}/_diag"
    [ -d "$diag" ] || return 0
    log "cleaning ${diag} logs"
    find "$diag" -type f \( -name '*.log' -o -name '*.log.*' -o -name 'Worker_*' -o -name 'Runner_*' \) -delete || true
    find "$diag" -type f -delete || true
}

cleanup_work() {
    local work="${RUNNER_DIR}/_work"
    [ -d "$work" ] || return 0
    log "removing leftover safe job dirs under ${work}"
    local entry name
    # Reserved runner internals under _work. Job workspaces (_temp, repo dirs,
    # _actions) are leftover once idle + cordoned.
    for entry in "$work"/* "$work"/.[!.]* "$work"/..?*; do
        [ -e "$entry" ] || continue
        name=$(basename "$entry")
        case "$name" in
            _tool|_update) continue ;;
        esac
        rm -rf "$entry" || log "warning: failed to remove ${entry}"
    done
    mkdir -p "$work"
}

cleanup_docker() {
    command -v docker >/dev/null 2>&1 || return 0
    if ! docker info >/dev/null 2>&1; then
        log "docker is installed but the daemon is not reachable; skip docker prune"
        return 0
    fi
    log "pruning leftover docker containers, builder cache, and unused images"
    docker container prune -f || log "warning: docker container prune failed"
    docker builder prune -af || log "warning: docker builder prune failed"
    docker image prune -af || log "warning: docker image prune failed"
}

chown_work() {
    local work="${RUNNER_DIR}/_work"
    mkdir -p "$work"
    if id "$RUNNER_USER" >/dev/null 2>&1; then
        log "chown -R ${RUNNER_USER}:${RUNNER_USER} ${work}"
        chown -R "$RUNNER_USER:$RUNNER_USER" "$work" || log "warning: chown ${work} failed"
    else
        log "user ${RUNNER_USER} does not exist; skip chown"
    fi
}

cleanup_diag || log "warning: _diag cleanup failed"
cleanup_work || log "warning: _work cleanup failed"
cleanup_docker || log "warning: docker cleanup failed"
chown_work || log "warning: _work chown failed"

log "cleanup finished; runner service will be started by EXIT trap"
ACTIONS_RUNNER_DISK_CLEANUP_SCRIPT
    chmod 0755 "$1/actions-runner-disk-cleanup"
    cat >"$1/actions-runner-disk-cleanup.service" <<'ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_SERVICE'
[Unit]
Description=GitHub Actions runner idle disk cleanup
Documentation=file:///opt/actions-runner
After=local-fs.target
# Do not Conflict= the runner service; this unit cordons it from ExecStart.

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/actions-runner-disk-cleanup
ExecStart=/usr/local/sbin/actions-runner-disk-cleanup
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=30min

[Install]
WantedBy=multi-user.target
ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_SERVICE
    chmod 0644 "$1/actions-runner-disk-cleanup.service"
    cat >"$1/actions-runner-disk-cleanup.timer" <<'ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_TIMER'
[Unit]
Description=Run idle runner disk cleanup every 6 hours
Requires=actions-runner-disk-cleanup.service

[Timer]
OnBootSec=30min
OnUnitActiveSec=6h
Persistent=true
RandomizedDelaySec=10min
AccuracySec=1min
Unit=actions-runner-disk-cleanup.service

[Install]
WantedBy=timers.target
ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_TIMER
    chmod 0644 "$1/actions-runner-disk-cleanup.timer"
    cat >"$1/actions-runner-disk-cleanup-pressure.service" <<'ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_PRESSURE_SERVICE'
[Unit]
Description=GitHub Actions runner idle disk cleanup (only if disk >= 70%)
Documentation=file:///opt/actions-runner
After=local-fs.target

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/actions-runner-disk-cleanup
ExecStart=/usr/local/sbin/actions-runner-disk-cleanup --if-pressure
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=30min

[Install]
WantedBy=multi-user.target
ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_PRESSURE_SERVICE
    chmod 0644 "$1/actions-runner-disk-cleanup-pressure.service"
    cat >"$1/actions-runner-disk-cleanup-pressure.timer" <<'ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_PRESSURE_TIMER'
[Unit]
Description=Check runner/Docker disk use every 15 minutes and clean if idle and >= 70%
Requires=actions-runner-disk-cleanup-pressure.service

[Timer]
OnBootSec=10min
OnUnitActiveSec=15min
Persistent=false
RandomizedDelaySec=2min
AccuracySec=1min
Unit=actions-runner-disk-cleanup-pressure.service

[Install]
WantedBy=timers.target
ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_PRESSURE_TIMER
    chmod 0644 "$1/actions-runner-disk-cleanup-pressure.timer"
    cat >"$1/install.sh" <<'ACTIONS_RUNNER_DISK_CLEANUP_INSTALL_SH'
#!/bin/bash
# Install the idle disk-cleanup oneshot service and timers.
# START_TIMERS=0  — enable timers only (Packer image build; do not fire now).
# START_TIMERS=1  — enable and start timers (guest-setup on a live clone).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DESTDIR="${DESTDIR:-}"
START_TIMERS="${START_TIMERS:-1}"
RUNNER_USER="${RUNNER_USER:-runner}"
RUNNER_DIR="${RUNNER_DIR:-/opt/actions-runner}"
DISK_CLEANUP_THRESHOLD="${DISK_CLEANUP_THRESHOLD:-70}"

install -d -m 0755 \
    "${DESTDIR}/usr/local/sbin" \
    "${DESTDIR}/etc/systemd/system" \
    "${DESTDIR}/etc/default"
install -m 0755 "$HERE/actions-runner-disk-cleanup" "${DESTDIR}/usr/local/sbin/actions-runner-disk-cleanup"
install -m 0644 "$HERE/actions-runner-disk-cleanup.service" "${DESTDIR}/etc/systemd/system/actions-runner-disk-cleanup.service"
install -m 0644 "$HERE/actions-runner-disk-cleanup.timer" "${DESTDIR}/etc/systemd/system/actions-runner-disk-cleanup.timer"
install -m 0644 "$HERE/actions-runner-disk-cleanup-pressure.service" "${DESTDIR}/etc/systemd/system/actions-runner-disk-cleanup-pressure.service"
install -m 0644 "$HERE/actions-runner-disk-cleanup-pressure.timer" "${DESTDIR}/etc/systemd/system/actions-runner-disk-cleanup-pressure.timer"

cat >"${DESTDIR}/etc/default/actions-runner-disk-cleanup" <<EOF
RUNNER_USER=${RUNNER_USER}
RUNNER_DIR=${RUNNER_DIR}
DISK_CLEANUP_THRESHOLD=${DISK_CLEANUP_THRESHOLD}
EOF
chmod 0644 "${DESTDIR}/etc/default/actions-runner-disk-cleanup"

if [ -n "$DESTDIR" ]; then
    echo "Installed disk-cleanup units under ${DESTDIR} (skipped systemctl)"
    exit 0
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable actions-runner-disk-cleanup.timer
    systemctl enable actions-runner-disk-cleanup-pressure.timer
    if [ "$START_TIMERS" = 1 ]; then
        systemctl start actions-runner-disk-cleanup.timer
        systemctl start actions-runner-disk-cleanup-pressure.timer
        echo "Enabled and started actions-runner-disk-cleanup timers"
    else
        echo "Enabled actions-runner-disk-cleanup timers (not started; START_TIMERS=0)"
    fi
else
    echo "systemctl not available; files installed under /etc/systemd/system"
fi
ACTIONS_RUNNER_DISK_CLEANUP_INSTALL_SH
    chmod 0755 "$1/install.sh"
}

install_runner_disk_cleanup() {
    local src="${DISK_CLEANUP_SRC:-}"
    local dir
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    for dir in ${src:+"$src"} /tmp/actions-runner-disk-cleanup "$script_dir/disk-cleanup"; do
        [ -n "$dir" ] || continue
        if [ -f "$dir/install.sh" ]; then
            echo "Installing runner disk-cleanup timers from ${dir}"
            START_TIMERS=1 RUNNER_USER="$RUNNER_USER" RUNNER_DIR="$RUNNER_DIR" bash "$dir/install.sh"
            return
        fi
    done
    echo "Bundling runner disk-cleanup assets (not uploaded next to guest-setup)"
    dir=/tmp/actions-runner-disk-cleanup
    write_bundled_disk_cleanup "$dir"
    START_TIMERS=1 RUNNER_USER="$RUNNER_USER" RUNNER_DIR="$RUNNER_DIR" bash "$dir/install.sh"
}

cleanup() {
    rm -f "$ENV_FILE" /tmp/actions-runner.tgz
}
trap cleanup EXIT

if command -v cloud-init >/dev/null 2>&1; then
    echo "cloud-init status: $(cloud-init status 2>/dev/null || true)"
    if command -v timeout >/dev/null 2>&1; then
        if ! timeout 180 cloud-init status --wait; then
            echo "cloud-init still running after 180s; continuing"
        fi
    else
        echo "timeout(1) not available; not waiting unbounded for cloud-init"
    fi
fi

if [ -d /opt/post-generation ] && [ ! -f /opt/post-generation/.fleet-done ]; then
    echo "Running /opt/post-generation scripts"
    find /opt/post-generation -mindepth 1 -maxdepth 1 -type f -name '*.sh' -exec bash {} \;
    touch /opt/post-generation/.fleet-done
fi

if ! id "$RUNNER_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$RUNNER_USER"
fi

# Packer installs Docker as the build user; that account is deprovisioned.
# The systemd runner runs as ciuser (runner) and needs the docker group
# before svc.sh starts so jobs can use unix:///var/run/docker.sock.
in_docker_group=0
if id -nG "$RUNNER_USER" | grep -qw docker; then
    in_docker_group=1
fi
if [ "$in_docker_group" -eq 0 ]; then
    if ! getent group docker >/dev/null 2>&1; then
        if command -v docker >/dev/null 2>&1 || [ -e /var/run/docker.sock ]; then
            groupadd --system docker
        else
            echo "Docker is not installed; not creating the docker group"
        fi
    fi
    if getent group docker >/dev/null 2>&1; then
        usermod -aG docker "$RUNNER_USER"
        echo "Added ${RUNNER_USER} to docker group"
    fi
fi

install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0755 "$RUNNER_DIR" "${RUNNER_DIR}/_work"
chown -R "$RUNNER_USER:$RUNNER_USER" "${RUNNER_DIR}/_work"
cd "$RUNNER_DIR"

if [ ! -x "$RUNNER_DIR/config.sh" ]; then
    echo "Downloading actions/runner from ${RUNNER_TARBALL_URL}"
    curl -fsSL -o /tmp/actions-runner.tgz "$RUNNER_TARBALL_URL"
    tar -xzf /tmp/actions-runner.tgz -C "$RUNNER_DIR"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR"
fi

if [ -f "$RUNNER_DIR/.runner" ]; then
    echo "Runner ${RUNNER_NAME} is already configured"
else
    echo "Registering runner ${RUNNER_NAME}"
    sudo -u "$RUNNER_USER" -- "$RUNNER_DIR/config.sh" \
        --unattended \
        --url "$RUNNER_URL" \
        --token "$RUNNER_TOKEN" \
        --name "$RUNNER_NAME" \
        --labels "$RUNNER_LABELS" \
        --work _work \
        --replace
fi

# Docker/root leftover files in _work cause EACCES on checkout unlink.
# guest-setup runs as root; the job-started hook uses a NOPASSWD sudo
# wrapper so the runner user can chown before each job.
WORK_DIR="${RUNNER_DIR}/_work"
HOOK_DIR="${RUNNER_DIR}/hooks"
HOOK_SCRIPT="${HOOK_DIR}/job-started.sh"
CHOWN_BIN="/usr/local/sbin/actions-runner-chown-work"
SUDOERS="/etc/sudoers.d/actions-runner-work"
hook_restart=0

install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0755 "$WORK_DIR" "$HOOK_DIR"
chown -R "$RUNNER_USER:$RUNNER_USER" "$WORK_DIR"
echo "Ensured ${WORK_DIR} is owned by ${RUNNER_USER}"

cat >"$CHOWN_BIN" <<EOF
#!/bin/bash
set -euo pipefail
# Fleet-installed. Only the runner _work tree; invoked via sudo from the hook.
WORK='$WORK_DIR'
OWNER='$RUNNER_USER'
mkdir -p "\$WORK"
chown -R "\$OWNER:\$OWNER" "\$WORK"
EOF
chmod 0755 "$CHOWN_BIN"
chown root:root "$CHOWN_BIN"

cat >"$HOOK_SCRIPT" <<EOF
#!/bin/bash
set -euo pipefail
sudo --non-interactive $CHOWN_BIN
EOF
chmod 0755 "$HOOK_SCRIPT"
chown "$RUNNER_USER:$RUNNER_USER" "$HOOK_SCRIPT"

cat >"$SUDOERS" <<EOF
Defaults:${RUNNER_USER} !requiretty
${RUNNER_USER} ALL=(root) NOPASSWD: ${CHOWN_BIN}
EOF
chmod 0440 "$SUDOERS"
if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$SUDOERS"; then
    echo "invalid sudoers ${SUDOERS}" >&2
    exit 1
fi

ENV_RUNNER="${RUNNER_DIR}/.env"
touch "$ENV_RUNNER"
wanted="ACTIONS_RUNNER_HOOK_JOB_STARTED=${HOOK_SCRIPT}"
if ! grep -qxF "$wanted" "$ENV_RUNNER"; then
    if grep -q '^ACTIONS_RUNNER_HOOK_JOB_STARTED=' "$ENV_RUNNER"; then
        sed -i "s|^ACTIONS_RUNNER_HOOK_JOB_STARTED=.*|${wanted}|" "$ENV_RUNNER"
    else
        echo "$wanted" >>"$ENV_RUNNER"
    fi
    hook_restart=1
    echo "Set ${wanted}"
fi
chown "$RUNNER_USER:$RUNNER_USER" "$ENV_RUNNER"
chmod 0644 "$ENV_RUNNER"

install_runner_disk_cleanup

if [ ! -f "$RUNNER_DIR/.service" ]; then
    "$RUNNER_DIR/svc.sh" install "$RUNNER_USER"
elif { [ "$in_docker_group" -eq 0 ] && getent group docker >/dev/null 2>&1; } || [ "$hook_restart" -eq 1 ]; then
    echo "Restarting runner service so docker group / job hook apply"
    "$RUNNER_DIR/svc.sh" stop || true
fi
"$RUNNER_DIR/svc.sh" start || true

echo "Fleet guest setup complete for ${RUNNER_NAME}"
