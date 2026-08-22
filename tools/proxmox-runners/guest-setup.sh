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

if [ ! -f "$RUNNER_DIR/.service" ]; then
    "$RUNNER_DIR/svc.sh" install "$RUNNER_USER"
elif { [ "$in_docker_group" -eq 0 ] && getent group docker >/dev/null 2>&1; } || [ "$hook_restart" -eq 1 ]; then
    echo "Restarting runner service so docker group / job hook apply"
    "$RUNNER_DIR/svc.sh" stop || true
fi
"$RUNNER_DIR/svc.sh" start || true

echo "Fleet guest setup complete for ${RUNNER_NAME}"
