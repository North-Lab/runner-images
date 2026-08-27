# Proxmox runner fleet

Python CLI that copies the Ubuntu 26.04 Packer template to each local-storage node once, full-clones runner VMs from that node's replica in parallel, then registers GitHub Actions runners. Runners are never cloned on the template node and migrated.

See [Deploy a runner fleet](../../docs/create-image-and-proxmox-resources.md#deploy-a-runner-fleet) (template-copy-then-parallel-clone) and [Automatic disk cleanup](../../docs/create-image-and-proxmox-resources.md#automatic-disk-cleanup).

```bash
cp fleet.example.toml fleet.toml   # gitignored; do not commit tokens
python3 proxmox-runners.py deploy --config fleet.toml --count 6
# After rebuilding the Packer template, refresh per-node replicas:
python3 proxmox-runners.py deploy --config fleet.toml --count 6 --recreate-templates
```

`--recreate-templates` (or `[proxmox].recreate_templates = true`) deletes `ubuntu-2604-runner-<node>` replica templates, waits for each Proxmox delete task (UPID) to finish and confirms the replica is gone, then copies the current source template to each node again. Default is off (reuse replicas). It does not delete the source Packer template or runner VMs.
