source "proxmox-iso" "ubuntu" {
  proxmox_url              = var.proxmox_url
  username                 = var.proxmox_username
  token                    = var.proxmox_token != "" ? var.proxmox_token : null
  password                 = var.proxmox_token == "" && var.proxmox_password != "" ? var.proxmox_password : null
  insecure_skip_tls_verify = var.proxmox_insecure_skip_tls_verify
  node                     = var.proxmox_node
  pool                     = var.proxmox_pool != "" ? var.proxmox_pool : null
  task_timeout             = var.proxmox_task_timeout

  vm_name                  = var.vm_name
  vm_id                    = var.vm_id
  tags                     = var.template_tags
  template_name            = var.template_name
  template_description     = local.template_description
  skip_convert_to_template = var.skip_convert_to_template

  os                 = "l26"
  bios               = var.bios
  machine            = var.machine
  qemu_agent         = var.qemu_agent
  scsi_controller    = var.scsi_controller
  cores              = var.vm_cores
  sockets            = var.vm_sockets
  cpu_type           = var.vm_cpu_type
  memory             = var.vm_memory
  ballooning_minimum = var.vm_ballooning_minimum
  onboot             = false

  dynamic "efi_config" {
    for_each = var.bios == "ovmf" ? [1] : []
    content {
      efi_storage_pool  = var.disk_storage_pool
      efi_type          = var.efi_type
      pre_enrolled_keys = var.efi_pre_enrolled_keys
    }
  }

  disks {
    type         = var.disk_type
    storage_pool = var.disk_storage_pool
    disk_size    = var.disk_size
    format       = var.disk_format
    cache_mode   = var.disk_cache_mode
    discard      = var.disk_discard
    ssd          = var.disk_type == "virtio" ? false : var.disk_ssd
  }

  network_adapters {
    model    = var.network_model
    bridge   = var.network_bridge
    vlan_tag = var.network_vlan_tag
    firewall = var.network_firewall
    mtu      = var.network_mtu
  }

  cloud_init              = var.cloud_init
  cloud_init_storage_pool = local.cloud_init_storage_pool
  cloud_init_disk_type    = var.cloud_init_disk_type

  boot_iso {
    type             = var.boot_iso_type
    iso_file         = var.iso_file != "" ? var.iso_file : null
    iso_url          = var.iso_file == "" ? var.iso_url : null
    iso_checksum     = var.iso_checksum
    iso_storage_pool = var.iso_storage_pool
    iso_download_pve = var.iso_download_pve
    unmount          = var.iso_unmount
  }

  dynamic "additional_iso_files" {
    for_each = var.autoinstall_delivery == "iso" ? [1] : []
    content {
      type             = "scsi"
      iso_storage_pool = var.iso_storage_pool
      unmount          = true
      cd_label         = "cidata"
      cd_files = [
        "${path.root}/http/user-data",
        "${path.root}/http/meta-data",
        "${path.root}/http/vendor-data"
      ]
    }
  }

  http_directory    = "${path.root}/http"
  http_bind_address = var.http_bind_address != "" ? var.http_bind_address : null
  http_interface    = var.http_interface != "" ? var.http_interface : null
  http_port_min     = var.http_port_min
  http_port_max     = var.http_port_max

  boot_wait    = var.boot_wait
  boot_command = local.boot_command

  ssh_username              = var.ssh_username
  ssh_password              = var.ssh_password
  ssh_timeout               = var.ssh_timeout
  ssh_handshake_attempts    = var.ssh_handshake_attempts
  ssh_clear_authorized_keys = var.ssh_clear_authorized_keys
}
