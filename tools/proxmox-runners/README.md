# Proxmox runner fleet

Python CLI that copies the Ubuntu 26.04 Packer template to each local-storage node once, full-clones runner VMs from that node's replica in parallel, then registers GitHub Actions runners. Runners are never cloned on the template node and migrated.

See [Deploy a runner fleet](../../docs/create-image-and-proxmox-resources.md#deploy-a-runner-fleet) (template-copy-then-parallel-clone) and [Automatic disk cleanup](../../docs/create-image-and-proxmox-resources.md#automatic-disk-cleanup).

```bash
cp fleet.example.toml fleet.toml   # gitignored; do not commit tokens
python3 proxmox-runners.py deploy --config fleet.toml --count 6
```
