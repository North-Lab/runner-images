#!/bin/bash -e
################################################################################
##  File:  deprovision-proxmox.sh
##  Desc:  Seal a runner image for use as a Proxmox VM template.
##         Replaces the Azure waagent deprovision step.
################################################################################

echo "Preparing guest for Proxmox template conversion"

# Subiquity leaves installer cloud-init/GRUB overrides that would re-run
# autoinstall on the first boot of every clone.
rm -f /etc/cloud/cloud.cfg.d/99-installer.cfg
rm -f /etc/cloud/cloud.cfg.d/subiquity-disable-cloudinit-networking.cfg
rm -f /etc/cloud/cloud.cfg.d/90-installer-network.cfg

# Prefer Proxmox Cloud-Init (NoCloud + ConfigDrive) on clones.
cat >/etc/cloud/cloud.cfg.d/99-proxmox.cfg <<'EOF'
datasource_list: [ NoCloud, ConfigDrive, None ]
manage_etc_hosts: true
EOF

# Drop leftover autoinstall kernel arguments so clones boot normally.
if [ -f /etc/default/grub ]; then
    sed -i -E 's/ ?autoinstall(=[^ ]*)?//g; s/ ?ds=nocloud[^ ]*//g' /etc/default/grub
    if command -v update-grub >/dev/null 2>&1; then
        update-grub
    fi
fi

if [ -d /boot/grub ]; then
    find /boot/grub -name '*.cfg' -o -name 'grubenv' | while read -r grubfile; do
        sed -i -E 's/ ?autoinstall(=[^ ]*)?//g; s/ ?ds=nocloud[^ ]*//g' "$grubfile" || true
    done
fi

# qemu-guest-agent is required for Proxmox IP detection and graceful shutdown.
if ! command -v qemu-ga >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y qemu-guest-agent
fi
systemctl enable qemu-guest-agent
systemctl start qemu-guest-agent || true

if command -v cloud-init >/dev/null 2>&1; then
    cloud-init clean --logs --seed || cloud-init clean --logs
fi

# Unique machine-id / SSH host keys per clone.
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*

# Do not carry the build password into clones. Cloud-Init on the clone
# should create the runner user (see docs/create-image-and-proxmox-resources.md).
if id packer >/dev/null 2>&1; then
    passwd -l packer || true
    rm -f /home/packer/.ssh/authorized_keys
fi

export HISTSIZE=0
sync
echo "Proxmox deprovision complete"
