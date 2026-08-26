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
- [Deploy a runner fleet](#deploy-a-runner-fleet)
- [Automatic disk cleanup](#automatic-disk-cleanup)
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

The template enables an idle disk-cleanup systemd timer (6 hours, plus a 15-minute check when disk is at least 70%). See [Automatic disk cleanup](#automatic-disk-cleanup). Clone that template for each self-hosted runner. Do not run jobs on the template itself.

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
cp images/ubuntu/templates-proxmox/example.pkrvars.hcl \
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

After a successful build, Proxmox shows a template named `template_name` (default `ubuntu-2604-runner`) on `proxmox_node`. Packer converts the VM to a template automatically.

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

Install and register [actions/runner](https://github.com/actions/runner) on the clone after post-generation, or use the [fleet tool](#deploy-a-runner-fleet) to do this on every node.

## Deploy a runner fleet

`tools/proxmox-runners/proxmox-runners.py` clones the Packer template, spreads VMs across online Proxmox nodes, runs `/opt/post-generation`, and registers `actions/runner` as a systemd service. One command plus a gitignored config is enough for a three-node lab.

### Prerequisites

- The Ubuntu 26.04 template already built (see [Build the template](#build-the-template)).
- Python 3.11+ on the machine that talks to the Proxmox API (stdlib only; no pip packages).
- The same Proxmox API token style as Packer (`user@realm!tokenid` + `PROXMOX_TOKEN`).
- A GitHub PAT in `GITHUB_TOKEN` or `GH_TOKEN`. It is used only to mint a **short-lived registration token per VM**.
  - Repo runners: classic `repo` scope (or fine-grained Administration: Read and write).
  - Org runners: classic `admin:org` (or Organization self-hosted runners).
- qemu-guest-agent working on clones (the template enables it). The tool execs as root through the agent; SSH is optional.
- The Packer token can usually clone and start VMs but **not** talk to the guest agent. Fleet deploy needs extra privileges (PVE 9 guest-agent ACL, or `VM.Monitor` on PVE 8). `qm agent <vmid> ping` uses root on the node and can succeed while the API token gets HTTP 401/403.

```bash
pveum role add RunnerFleet -privs "VM.GuestAgent.Audit,VM.GuestAgent.FileRead,VM.GuestAgent.FileWrite,VM.GuestAgent.Unrestricted"
pveum acl modify / --token 'packer@pve!imagegen' --role RunnerFleet
```

Replace `packer@pve!imagegen` with the token id you put in `PROXMOX_USERNAME`. On PVE 8, grant `VM.Monitor` instead of the `VM.GuestAgent.*` names if those privileges do not exist. The CLI POSTs `/nodes/{node}/qemu/{vmid}/agent/ping` (GET fallback) and exits immediately on 401/403 instead of retrying.

Do not commit tokens. Copy the example config:

```bash
cp tools/proxmox-runners/fleet.example.toml tools/proxmox-runners/fleet.toml
```

`tools/proxmox-runners/fleet.toml` is gitignored. Prefer leaving `token` fields empty and exporting `PROXMOX_TOKEN` and `GITHUB_TOKEN`.

### Storage assumption

Proxmox **cannot** clone a template whose disks live on non-shared storage (`local-lvm`, local `dir`) directly onto another node (`qm clone --target` requires shared storage).

| Template disk storage | What the tool does |
| --------------------- | ------------------ |
| Shared (`nfs`, `ceph`/`rbd`, `cifs`, … `shared=1`) | Full clone with `target=<node>`. |
| Node-local (`local-lvm`, typical home lab) | Full clone on the template node, then **offline migrate** with `with-local-disks=1` to create a template replica on each target node. Runner VMs are then cloned locally on that node. |

Each target node must already have a storage with the **same id** as `[proxmox].storage` (default `local-lvm`) and `content` including `images`. If a node cannot receive that disk, the tool exits with the storages that node does have. It does not invent a storage or silently place every VM on the template node.

### Discover nodes

Node names are not hardcoded. The tool calls `GET /nodes` and uses **online** nodes. Optional `[proxmox].nodes` or `--nodes pve1,pve2,pve3` is an allowlist; any allowlisted node that is missing or offline is an error.

```bash
export PROXMOX_URL PROXMOX_USERNAME PROXMOX_TOKEN
python3 tools/proxmox-runners/proxmox-runners.py nodes --config tools/proxmox-runners/fleet.toml
```

### Roll out

`--count 6` on three online nodes places two VMs per node (round-robin: `gh-runner-01` … `gh-runner-06`). `--per-node 2` is the same if three nodes are online.

```bash
export PROXMOX_URL PROXMOX_USERNAME PROXMOX_TOKEN
export GITHUB_TOKEN

python3 tools/proxmox-runners/proxmox-runners.py deploy \
  --config tools/proxmox-runners/fleet.toml \
  --count 6
```

For each VM the tool:

1. Allocates a unique VMID (`cluster/nextid`, or `[vm].vmid_start`).
2. Full-clones the template (or the per-node replica).
3. Applies Cloud-Init (`ciuser`, optional SSH key, `ip=dhcp` or `ip=192.168.1.{n}/24,gw=...`).
4. Starts the VM and waits for qemu-guest-agent.
5. Runs `/opt/post-generation` as root (once per VM; stamped with `.fleet-done`).
6. Downloads `actions/runner` **linux-x64** (latest release unless `runner_version` is pinned).
7. Registers with a **fresh** registration token (`POST .../actions/runners/registration-token`) for the repo or org in `[github].url`.
8. Adds `ciuser` (default `runner`) to the `docker` group if Docker is installed, then installs the systemd service with `svc.sh install` / `svc.sh start`.
9. Installs the idle disk-cleanup oneshot + timers (`actions-runner-disk-cleanup.timer` every 6 hours, `actions-runner-disk-cleanup-pressure.timer` every 15 minutes when disk is at least 70%). Already-deployed VMs pick this up on the next `deploy` / guest-setup. The job-started `_work` chown hook is unchanged.

Proxmox Cloud-Init (`ciuser`) cannot set supplementary groups. guest-setup is the reliable path so Actions jobs can use `unix:///var/run/docker.sock`.

**Already-running fleet VMs** (created before this group fix) keep their registration. On each guest:

```bash
usermod -aG docker runner
# The running actions.runner service does not pick up new groups until restart:
systemctl restart 'actions.runner.*.service'
```

Confirm with `id runner` (should list `docker`) and `sg docker -c 'docker info'`. Re-running `deploy` also adds the group and restarts the service when the user was not already a member.

Docker/root leftover files in the runner workspace can make checkout fail with `EACCES unlink .../APKINDEX.tar.gz`. New deploys create `/opt/actions-runner/_work` as `runner` and install an `ACTIONS_RUNNER_HOOK_JOB_STARTED` hook that `chown -R`s that tree (via a NOPASSWD sudo wrapper limited to that script). **Already-deployed VMs** do not need a redeploy for the immediate fix:

```bash
chown -R runner:runner /opt/actions-runner/_work
```

Re-running `deploy` (or guest-setup) installs the hook so later jobs clean leftovers automatically.

Default labels: `self-hosted,linux,x64,ubuntu-26.04`. Add extras with `[github].extra_labels` or `--labels`.

Existing VMs with the same name are reused (not recloned). Guests that already have `/opt/actions-runner/.runner` skip `config.sh`. Online GitHub registrations with that name are left in place; guest setup still starts the service if needed.

## Automatic disk cleanup

Self-hosted clones fill `/opt/actions-runner/_work`, `_diag`, and Docker storage across jobs. The image and guest-setup install a systemd oneshot that **never interrupts a running job**:

1. A 6-hour timer always tries to clean when idle.
2. A 15-minute timer cleans only when idle **and** utilization is at least 70% on the filesystems that hold `/opt/actions-runner` or Docker data (`docker info` root, else `/var/lib/docker`).
3. If `Runner.Worker` is running, the script exits 0.
4. After idle is confirmed, it re-checks, then stops `actions.runner.*.service` (cordon) so a new job cannot land mid-clean.
5. While cordoned it removes `_diag` logs, leftover safe `_work` job dirs (`_temp`, `_actions`, repo workspaces — not `_tool` / `_update`), leftover Docker containers / builder cache / unused images when Docker is present, and `chown -R runner:runner /opt/actions-runner/_work` (`ciuser`). It does **not** delete `.runner`, `bin/`, or `svc.sh` / `.service`.
6. An `EXIT` trap always starts the runner service again, even if a cleanup step fails.

Units: `actions-runner-disk-cleanup.service` / `.timer` and `actions-runner-disk-cleanup-pressure.service` / `.timer`. Script: `/usr/local/sbin/actions-runner-disk-cleanup`. Defaults: `/etc/default/actions-runner-disk-cleanup`.

- **New templates:** Packer copies `tools/proxmox-runners/disk-cleanup/` and runs `install.sh` with `START_TIMERS=0` (enable on boot, do not fire during the image build) just before `deprovision-proxmox.sh`.
- **Existing clones:** re-run `deploy` (or guest-setup). The fleet CLI uploads the same files; guest-setup also has a bundled copy so a lone `guest-setup.sh` still installs the timers.

The `ACTIONS_RUNNER_HOOK_JOB_STARTED` `_work` chown hook from guest-setup is separate and is left in place.

Manual run (idle only; skips if a job is running):

```bash
sudo /usr/local/sbin/actions-runner-disk-cleanup
sudo /usr/local/sbin/actions-runner-disk-cleanup --if-pressure
systemctl list-timers 'actions-runner-disk-cleanup*'
```

```bash
python3 tools/proxmox-runners/proxmox-runners.py status --config tools/proxmox-runners/fleet.toml
```

### Teardown

```bash
python3 tools/proxmox-runners/proxmox-runners.py destroy \
  --config tools/proxmox-runners/fleet.toml \
  --yes
```

Stops and deletes VMs whose names start with `name_prefix` (default `gh-runner-`) and removes matching GitHub runner registrations. `--count N` limits that to `gh-runner-01` … `N`. `--keep-github` leaves GitHub registrations. Packer templates (including per-node replicas named `ubuntu-2604-runner-<node>`) are not deleted.

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
- **`systemctl enable cloud-init` fails in autoinstall late-commands** — Ubuntu 26 / cloud-init 25+ no longer ships `cloud-init.service`. Units are `cloud-init-main.service`, `cloud-init-local.service`, `cloud-init-network.service`, `cloud-config.service`, `cloud-final.service`, and `cloud-init.target` (enabled by the systemd generator when the `cloud-init` package is installed). `http/user-data` does not enable `cloud-init`; `deprovision-proxmox.sh` enables only units that exist and have an `[Install]` section. Do not add `systemctl enable cloud-init` back to late-commands.
- **Clones boot back into the installer** — leftover autoinstall kernel args. Confirm the build ran `deprovision-proxmox.sh` (it strips `autoinstall` / `ds=nocloud*` from GRUB).
- **Packer cannot find the guest IP** — install/enable `qemu-guest-agent` (autoinstall does this) and keep `qemu_agent = true`. Check the VM has a DHCP address on `network_bridge`.
- **API errors creating disks or ISOs** — token ACL, wrong `proxmox_node`, or a storage pool that does not allow the requested content type (`iso` vs `images`).
- **Out of space during provisioners** — raise `disk_size` above `75G`. The runner image is close to the Azure 75 GiB default.
- **OOM during provisioners** — raise `vm_memory`. 16 GiB matches the Azure build size.
- **`packer validate` on Azure templates** — keep using `images/ubuntu/templates`. The Proxmox plugin is only required under `images/ubuntu/templates-proxmox`, so Azure CI does not need it.
- **Fleet deploy: node cannot receive a disk** — `[proxmox].storage` must exist on that node with `images` content. For `local-lvm`, create the same storage id on every node. Shared storage must be mounted on every target.
- **Fleet deploy: qemu-guest-agent timeout** — first boot of this image is slow (cloud-init). The CLI waits up to 30 minutes and logs a heartbeat every ~30s. Each ping uses a short HTTP timeout so a down agent does not hang the API call. If the wait fails, the VM can still be running: on the node run `qm agent <vmid> ping`, and in the guest `systemctl status qemu-guest-agent`. Re-run `deploy`; existing VMs are reused by name.
- **Fleet deploy: HTTP 401/403 on agent/ping** — the guest agent is often fine (`qm agent <vmid> ping` works). The Packer API token lacks guest-agent privileges. Add the `RunnerFleet` role above and re-run; do not wait out the 30 minute timeout. The CLI POSTs `agent/ping` (PVE registers ping as POST) and falls back to GET.
- **Fleet deploy: GitHub registration token failed** — the PAT in `GITHUB_TOKEN` needs repo administration (repo runners) or org runner admin (org URL). The PAT is not the runner registration token; the tool mints those per VM.
- **Fleet jobs: permission denied on docker.sock** — the Cloud-Init user `runner` was not in the `docker` group (the image installs Docker for the Packer build user, which is deprovisioned). New deploys add `runner` to `docker` in `guest-setup.sh` before `svc.sh` starts. On existing VMs: `usermod -aG docker runner` then `systemctl restart 'actions.runner.*.service'`. Group changes do not apply to an already-running runner process. Do not recreate the VMs.
- **Fleet jobs: EACCES unlink under `_work`** — leftover files owned by root (often from Docker). Immediate fix on an already-deployed VM: `chown -R runner:runner /opt/actions-runner/_work`. New guest-setup installs a job-started hook that repeats that chown before each job. The idle disk-cleanup oneshot also chowns `_work` after it removes leftover job dirs.
- **Fleet disk fills up** — confirm `systemctl list-timers 'actions-runner-disk-cleanup*'` and `journalctl -u actions-runner-disk-cleanup.service -u actions-runner-disk-cleanup-pressure.service`. Cleanup is skipped (exit 0) while `Runner.Worker` is running. Re-run guest-setup to install the timers on VMs that predate this unit. Do not delete `/opt/actions-runner/.runner` or `bin/`.
