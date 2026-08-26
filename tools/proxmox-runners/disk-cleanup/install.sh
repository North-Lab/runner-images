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
