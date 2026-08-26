# Proxmox runner fleet

Python CLI that clones the Ubuntu 26.04 Packer template across a Proxmox cluster and registers GitHub Actions runners.

See [Deploy a runner fleet](../../docs/create-image-and-proxmox-resources.md#deploy-a-runner-fleet).

```bash
cp fleet.example.toml fleet.toml   # gitignored; do not commit tokens
python3 proxmox-runners.py deploy --config fleet.toml --count 6
```
