#!/bin/bash -e
################################################################################
##  File:  configure-apt-sources.sh
##  Desc:  Configure apt sources with failover from Azure to Ubuntu archives.
################################################################################

source $HELPER_SCRIPTS/os.sh

touch /etc/apt/apt-mirrors.txt

printf "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n" | tee -a /etc/apt/apt-mirrors.txt
printf "https://archive.ubuntu.com/ubuntu/\tpriority:2\n" | tee -a /etc/apt/apt-mirrors.txt
printf "https://security.ubuntu.com/ubuntu/\tpriority:3\n" | tee -a /etc/apt/apt-mirrors.txt

if ! is_ubuntu22; then
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
        # Azure marketplace images use azure.archive; ISO/Proxmox installs use archive.ubuntu.com.
        if grep -q 'azure.archive.ubuntu.com' /etc/apt/sources.list.d/ubuntu.sources; then
            sed -i 's|http://azure\.archive\.ubuntu\.com/ubuntu/|mirror+file:/etc/apt/apt-mirrors.txt|' /etc/apt/sources.list.d/ubuntu.sources
        fi

        # Apt changes to survive Cloud Init
        if [ -d /etc/cloud/templates ]; then
            cp -f /etc/apt/sources.list.d/ubuntu.sources /etc/cloud/templates/sources.list.ubuntu.deb822.tmpl
        fi
    fi
else
    if [ -f /etc/apt/sources.list ]; then
        if grep -q 'azure.archive.ubuntu.com' /etc/apt/sources.list; then
            sed -i 's|http://azure\.archive\.ubuntu\.com/ubuntu/|mirror+file:/etc/apt/apt-mirrors.txt|' /etc/apt/sources.list
        fi

        # Apt changes to survive Cloud Init
        if [ -d /etc/cloud/templates ]; then
            cp -f /etc/apt/sources.list /etc/cloud/templates/sources.list.ubuntu.tmpl
        fi
    fi
fi
