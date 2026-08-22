# GitHub Actions Runner Images on Proxmox

This fork can build the Ubuntu 26.04 runner image as a **Proxmox VE VM template** using Packer. The software install pipeline is the same set of provisioners as the Azure Ubuntu 26.04 image. Only the builder, first-boot (autoinstall), and final deprovision step are Proxmox-specific.

Upstream Azure-only instructions remain in [create-image-and-azure-resources.md](create-image-and-azure-resources.md). Azure templates under `images/ubuntu/templates/` are unchanged as a build path; a few shared install scripts skip Azure-only files when they are absent (for example `/etc/waagent.conf`).

- [What you get](#what-you-get)
- [Prerequisites](#prerequisites)
- [Proxmox API token](#proxmox-api-token)
- [ISO](#iso)
- [Configure variables](#configure-variables)
- [Build the template](#build-the-template)
- [Required variables](#required-variables)
- [Optional variables](#optional-variables)
- [Clone the template into a runner VM](#clone-the-template-into-a-runner-vm)
- [Post-generation scripts](#post-generation-scripts)
- [How this differs from Azure](#how-this-differs-from-azure)
- [Troubleshooting](#troubleshooting)

## What you get

Packer uses the [hashicorp/proxmox](https://developer.hashicorp.com/packer/integrations/hashicorp/proxmox) `proxmox-iso` builder to:

1. Create a temporary VM on a Proxmox node.
2. Boot the Ubuntu 26.04 live-server ISO and install Ubuntu with Subiquity autoinstall.
3. SSH in and run the same `images/ubuntu/scripts/build` provisioners as `build.ubuntu-26_04.pkr.hcl`.
4. Seal the guest (cloud-init reset, machine-id, qemu-guest-agent) instead of `waagent -deprovision`.
5. Convert the VM to a Proxmox template (unless `skip_convert_to_template` is set).

Clone that template for each self-hosted runner. Do not run jobs on the template itself.

Ubuntu 26.04 **x86_64** is the supported Proxmox path. Arm64 on Proxmox is not wired up.

## Prerequisites

The machine that runs Packer (your laptop, a management VM, or a CI agent) needs:

- [Packer](https://developer.hashicorp.com/packer/install) **1.8.2 or later** (1.10+ recommended).
- The Proxmox plugin (installed by `packer init` from the template directory).
- Git, so you can clone this repository.
- Network reachability to the Proxmox API (`https://<pve>:8006`).
- When `autoinstall_delivery = "http"` (the default), the build VM must be able to reach Packer's temporary HTTP server on the Packer host. Set `http_bind_address` to an address the VM can route to, or use `autoinstall_delivery = "iso"` instead.
- `qemu-guest-agent` is installed during autoinstall and enabled again at seal time. Leave `qemu_agent = true` so Packer and Proxmox can read the guest IP.

On the Proxmox side you need:

- A node with enough free RAM and disk for the **build** VM (defaults: 4 vCPU, 16 GiB RAM, 75 GiB disk). The full runner-image install is large and can take several hours.
- A storage pool for VM disks (default `local-lvm`).
- A storage pool that allows **ISO** content (default `local`) for the Ubuntu ISO and, if you use seed-ISO autoinstall, a generated `cidata` ISO.
- A bridge (default `vmbr0`) with DHCP or other connectivity so the VM can install packages from the internet.
- An API token (preferred) or username/password. Token auth is documented below.

The official Ubuntu 26.04 live-server ISO filename is `ubuntu-26.04-live-server-amd64.iso` ([releases.ubuntu.com/26.04](https://releases.ubuntu.com/26.04/)).

## Proxmox API token

Create a dedicated user (or use an existing one) and an API token. Privilege separation tokens are recommended (`privsep=1`) with an ACL that can manage VMs and storage on the target node.

A typical lab token needs access to:

- `/nodes/<node>` — create and configure the build VM
- `/storage/<iso-pool>` — read (and upload) ISOs
- `/storage/<disk-pool>` — allocate the VM disk and Cloud-Init drive
- `/pools/<pool>` — only if you set `proxmox_pool`

In the UI: **Datacenter → Permissions → API Tokens**. The username you pass to Packer must include the realm and token id:

```text
packer@pve!imagegen
```

The token **secret** is shown once. Pass it as `proxmox_token` / `PROXMOX_TOKEN`. Do not commit it.

Password auth (`proxmox_password` / `PROXMOX_PASSWORD`) works if `proxmox_token` is empty. Token auth takes precedence when both are set.

For lab TLS certificates, set `proxmox_insecure_skip_tls_verify = true`.

## ISO

**Option A — already uploaded (recommended):** download the live-server ISO, upload it to an ISO-capable datastore, then set:

```hcl
iso_file         = "local:iso/ubuntu-26.04-live-server-amd64.iso"
iso_storage_pool = "local"
iso_checksum     = "file:https://releases.ubuntu.com/26.04/SHA256SUMS"
```

**Option B — Packer downloads it:** leave `iso_file` empty and set `iso_url` (the default already points at the official 26.04 live-server ISO). Packer uploads the file to `iso_storage_pool`. `iso_download_pve = true` makes the PVE node download the ISO itself.

## Configure variables

From the repository root:

```bash
cp images/ubuntu/templates-proxmox/proxmox.pkrvars.hcl.example \
   images/ubuntu/templates-proxmox/proxmox.pkrvars.hcl
```

Edit `proxmox.pkrvars.hcl` with your API URL, token, node, storage, bridge, and ISO path. That filename is gitignored.

You can also pass `-var` flags or environment variables (`PROXMOX_URL`, `PROXMOX_USERNAME`, `PROXMOX_TOKEN`, `PROXMOX_PASSWORD`).

Default autoinstall creates a build user `packer` / password `packer` (hashed in `http/user-data`). That account is locked at the end of the build. Change both the hash in `user-data` and `ssh_password` if you do not want the default during the build.

## Build the template

```bash
cd images/ubuntu/templates-proxmox

packer init .
packer validate -var-file=proxmox.pkrvars.hcl .

packer build -var-file=proxmox.pkrvars.hcl .
```

To build only this image if you later add more builds to the directory:

```bash
packer build -only 'ubuntu-26_04.proxmox-iso.ubuntu' -var-file=proxmox.pkrvars.hcl .
```

After a successful build, Proxmox shows a template named `template_name` (default `ubuntu-26.04-runner`) on `proxmox_node`. Packer converts the VM to a template automatically.

### Autoinstall delivery

| `autoinstall_delivery` | How it works | When to use |
| ---------------------- | ------------ | ----------- |
| `http` (default) | GRUB gets `ds=nocloud-net;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/`. Packer serves `http/user-data` and `http/meta-data`. | Packer host is reachable from the VM network. Set `http_bind_address`. |
| `iso` | Packer attaches a `cidata` ISO. GRUB gets `ds=nocloud`. | The VM cannot route back to Packer (isolated VLAN, no NAT). |

## Required variables

These must be set (via the example vars file, `-var`, or environment) for a real build:

| Template var | Env var | Description |
| ------------ | ------- | ----------- |
| `proxmox_url` | `PROXMOX_URL` | API URL including `/api2/json`. |
| `proxmox_username` | `PROXMOX_USERNAME` | `user@realm` or `user@realm!tokenid`. |
| `proxmox_token` or `proxmox_password` | `PROXMOX_TOKEN` / `PROXMOX_PASSWORD` | Token secret (preferred) or password. |
| `proxmox_node` | | Node name (not the DNS name) that will run the build VM. |
| `disk_storage_pool` | | Datastore for the VM disk. |
| `iso_file` or `iso_url` | | Installer ISO already on PVE, or a download URL. |
| `iso_storage_pool` | | Datastore with ISO content. |
| `network_bridge` | | Bridge for the build VM NIC. |

## Optional variables

Common overrides (all have defaults; see `variable.ubuntu-proxmox.pkr.hcl` for the full list):

- `vm_id` — pin the VMID (and template ID). Unset uses the next free ID.
- `vm_name`, `template_name`, `template_description`, `template_tags`
- `vm_cores`, `vm_sockets`, `vm_cpu_type`, `vm_memory` — build VM size
- `disk_size` — default `75G` (same as Azure Ubuntu 26.04)
- `bios` — `ovmf` (UEFI, default) or `seabios`
- `network_vlan_tag` — VLAN id; empty is untagged
- `cloud_init` — attach a Proxmox Cloud-Init drive on the template (default `true`)
- `cloud_init_storage_pool` — defaults to `disk_storage_pool`
- `qemu_agent` — default `true`
- `http_bind_address`, `http_interface`, `http_port_min`, `http_port_max`
- `autoinstall_delivery` — `http` or `iso`
- `proxmox_insecure_skip_tls_verify`
- `skip_convert_to_template` — leave a powered-off VM for debugging
- `image_os` — keep `ubuntu26`
- `image_version` — written into image metadata (default `dev`)
- `ssh_timeout` — wait for SSH after autoinstall (default `60m`)

Do not put lab-specific hosts, tokens, or VLANs in the committed example file. Copy it.

## Clone the template into a runner VM

The template is meant to be **cloned**, not started. Use full clones unless you know you want linked clones.

```bash
TEMPLATE_VMID=9000
RUNNER_VMID=9101
RUNNER_NAME=gh-runner-01

qm clone "$TEMPLATE_VMID" "$RUNNER_VMID" --name "$RUNNER_NAME" --full
qm set "$RUNNER_VMID" \
  --ciuser runner \
  --sshkeys /root/runner.pub \
  --ipconfig0 ip=dhcp \
  --agent enabled=1
qm resize "$RUNNER_VMID" scsi0 +50G   # optional
qm start "$RUNNER_VMID"
```

In the UI: right-click the template → **Clone**, then edit Cloud-Init (user, SSH keys, IP) before first boot.

Cloud-Init on the clone creates the user you set (`ciuser`). That user needs sudo for post-generation, matching the Azure Linux note.

Install and register [actions/runner](https://github.com/actions/runner) on the clone after post-generation. This repository only builds the disk image.

## Post-generation scripts

The same Ubuntu post-generation scripts as Azure are installed at `/opt/post-generation`. Run them on **each clone** as root after first boot, once Cloud-Init has created the runner user:

```bash
sudo su -c "find /opt/post-generation -mindepth 1 -maxdepth 1 -type f -name '*.sh' -exec bash {} \;"
```

Details are in [create-image-and-azure-resources.md](create-image-and-azure-resources.md#post-generation-scripts).

## How this differs from Azure

| Topic | Azure | Proxmox |
| ----- | ----- | ------- |
| Packer directory | `images/ubuntu/templates` | `images/ubuntu/templates-proxmox` |
| Builder | `azure-arm` | `proxmox-iso` |
| Base OS | Canonical marketplace image | Ubuntu 26.04 live-server ISO + autoinstall |
| Final cleanup | `waagent -force -deprovision+user` | `deprovision-proxmox.sh` |
| Artifact | Azure managed image / gallery version | Proxmox VM template |
| Guest agent | waagent | qemu-guest-agent |

Azure CLI, azcopy, and other tools from the upstream toolset are still installed. They are not Azure-only bootstrap.

## Troubleshooting

- **Autoinstall sits on the GRUB or language screen** — Packer's HTTP server is not reachable (`autoinstall_delivery = "http"`). Set `http_bind_address` to a VM-reachable IP, open the HTTP port range, or switch to `autoinstall_delivery = "iso"`.
- **Clones boot back into the installer** — leftover autoinstall kernel args. Confirm the build ran `deprovision-proxmox.sh` (it strips `autoinstall` / `ds=nocloud*` from GRUB).
- **Packer cannot find the guest IP** — install/enable `qemu-guest-agent` (autoinstall does this) and keep `qemu_agent = true`. Check the VM has a DHCP address on `network_bridge`.
- **API errors creating disks or ISOs** — token ACL, wrong `proxmox_node`, or a storage pool that does not allow the requested content type (`iso` vs `images`).
- **Out of space during provisioners** — raise `disk_size` above `75G`. The runner image is close to the Azure 75 GiB default.
- **OOM during provisioners** — raise `vm_memory`. 16 GiB matches the Azure build size.
- **`packer validate` on Azure templates** — keep using `images/ubuntu/templates`. The Proxmox plugin is only required under `images/ubuntu/templates-proxmox`, so Azure CI does not need it.
