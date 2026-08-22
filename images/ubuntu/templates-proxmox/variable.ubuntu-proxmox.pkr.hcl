// Proxmox API authentication. Prefer an API token (username includes the token id).
// Token example: username = "packer@pve!imagegen", token = "<uuid>"
// Password auth is supported as a fallback via proxmox_password / PROXMOX_PASSWORD.
variable "proxmox_url" {
  type        = string
  default     = "${env("PROXMOX_URL")}"
  description = "Proxmox API URL, including the full path (e.g. https://pve.lab.example:8006/api2/json)."
}

variable "proxmox_username" {
  type        = string
  default     = "${env("PROXMOX_USERNAME")}"
  description = "Proxmox username including realm. For tokens, append !<tokenid> (e.g. packer@pve!imagegen)."
}

variable "proxmox_token" {
  type        = string
  default     = "${env("PROXMOX_TOKEN")}"
  sensitive   = true
  description = "Proxmox API token secret. Preferred over password auth."
}

variable "proxmox_password" {
  type        = string
  default     = "${env("PROXMOX_PASSWORD")}"
  sensitive   = true
  description = "Proxmox user password. Used only when proxmox_token is empty."
}

variable "proxmox_insecure_skip_tls_verify" {
  type        = bool
  default     = false
  description = "Skip TLS verification for the Proxmox API (lab certificates)."
}

variable "proxmox_node" {
  type        = string
  default     = ""
  description = "Proxmox node name that will host the build VM."
}

variable "proxmox_pool" {
  type        = string
  default     = ""
  description = "Optional Proxmox resource pool for the build VM / template."
}

variable "proxmox_task_timeout" {
  type        = string
  default     = "15m"
  description = "Timeout for long Proxmox API tasks such as template conversion."
}

// VM / template identity
variable "vm_name" {
  type        = string
  default     = "ubuntu-2604-runner"
  description = "Name of the temporary build VM."
}

variable "vm_id" {
  type        = number
  default     = null
  description = "Optional VMID for the build VM and resulting template. Leave unset to use the next free ID."
}

variable "template_name" {
  type        = string
  default     = "ubuntu-2604-runner"
  description = "Name of the Proxmox VM template created at the end of the build."
}

variable "template_description" {
  type        = string
  default     = ""
  description = "Optional template description. Defaults to a generated Ubuntu 26.04 runner-images note."
}

variable "template_tags" {
  type        = string
  default     = "ubuntu-26.04;runner-images"
  description = "Semicolon-separated Proxmox tags applied to the VM/template."
}

variable "skip_convert_to_template" {
  type        = bool
  default     = false
  description = "Leave the finished VM as a regular VM instead of converting it to a template."
}

// Compute and disk
variable "vm_cores" {
  type        = number
  default     = 4
  description = "vCPU cores for the build VM. Clones can use a different size."
}

variable "vm_sockets" {
  type        = number
  default     = 1
  description = "CPU sockets for the build VM."
}

variable "vm_cpu_type" {
  type        = string
  default     = "host"
  description = "CPU type to emulate. host is recommended on a home-lab hypervisor."
}

variable "vm_memory" {
  type        = number
  default     = 16384
  description = "Memory in MiB for the build VM. The full runner-image install is heavy; 16384 matches Azure Standard_D4s_v4."
}

variable "vm_ballooning_minimum" {
  type        = number
  default     = 0
  description = "Minimum balloon memory in MiB. 0 disables ballooning."
}

variable "disk_storage_pool" {
  type        = string
  default     = "local-lvm"
  description = "Proxmox storage pool for the VM disk (and EFI disk when UEFI is enabled)."
}

variable "disk_size" {
  type        = string
  default     = "75G"
  description = "OS disk size. Matches the Azure Ubuntu 26.04 os_disk_size_gb default."
}

variable "disk_type" {
  type        = string
  default     = "scsi"
  description = "Disk bus type (scsi, virtio, sata, ide)."
}

variable "disk_format" {
  type        = string
  default     = "raw"
  description = "Disk image format (raw, qcow2, ...)."
}

variable "disk_cache_mode" {
  type        = string
  default     = "none"
  description = "Disk cache mode."
}

variable "disk_discard" {
  type        = bool
  default     = true
  description = "Enable TRIM/discard on the VM disk."
}

variable "disk_ssd" {
  type        = bool
  default     = true
  description = "Present the disk as SSD. Ignored for virtio disks."
}

variable "scsi_controller" {
  type        = string
  default     = "virtio-scsi-pci"
  description = "SCSI controller model."
}

variable "bios" {
  type        = string
  default     = "ovmf"
  description = "Firmware: ovmf (UEFI, recommended) or seabios."
}

variable "machine" {
  type        = string
  default     = "q35"
  description = "QEMU machine type (q35 or pc)."
}

variable "efi_pre_enrolled_keys" {
  type        = bool
  default     = false
  description = "Pre-enroll Microsoft Secure Boot keys on the EFI disk. Leave false unless you need Secure Boot."
}

variable "efi_type" {
  type        = string
  default     = "4m"
  description = "OVMF firmware size (4m or 2m)."
}

// Network
variable "network_bridge" {
  type        = string
  default     = "vmbr0"
  description = "Proxmox Linux bridge for the build VM NIC."
}

variable "network_model" {
  type        = string
  default     = "virtio"
  description = "NIC model."
}

variable "network_vlan_tag" {
  type        = string
  default     = ""
  description = "Optional VLAN tag. Empty means untagged."
}

variable "network_firewall" {
  type        = bool
  default     = false
  description = "Enable the Proxmox firewall on the build VM NIC."
}

variable "network_mtu" {
  type        = number
  default     = 0
  description = "NIC MTU. 0 uses the Proxmox default."
}

variable "qemu_agent" {
  type        = bool
  default     = true
  description = "Enable the QEMU guest agent on the VM. Autoinstall installs qemu-guest-agent."
}

variable "cloud_init" {
  type        = bool
  default     = true
  description = "Attach an empty Proxmox Cloud-Init drive after converting the VM to a template."
}

variable "cloud_init_storage_pool" {
  type        = string
  default     = ""
  description = "Storage pool for the Cloud-Init drive. Defaults to the boot disk pool when empty."
}

variable "cloud_init_disk_type" {
  type        = string
  default     = "scsi"
  description = "Cloud-Init disk bus type (scsi, sata, or ide)."
}

// ISO / autoinstall
variable "iso_file" {
  type        = string
  default     = ""
  description = "Existing Proxmox ISO path (e.g. local:iso/ubuntu-26.04-live-server-amd64.iso). Takes precedence over iso_url."
}

variable "iso_url" {
  type        = string
  default     = "https://releases.ubuntu.com/26.04/ubuntu-26.04-live-server-amd64.iso"
  description = "Ubuntu 26.04 live-server ISO URL used when iso_file is empty. Packer uploads it to iso_storage_pool."
}

variable "iso_checksum" {
  type        = string
  default     = "file:https://releases.ubuntu.com/26.04/SHA256SUMS"
  description = "ISO checksum. file: URL form is supported."
}

variable "iso_storage_pool" {
  type        = string
  default     = "local"
  description = "Proxmox storage pool that holds ISOs (must allow ISO content)."
}

variable "iso_download_pve" {
  type        = bool
  default     = false
  description = "When using iso_url, download the ISO on the PVE node instead of through the Packer host."
}

variable "iso_unmount" {
  type        = bool
  default     = true
  description = "Detach the installer ISO from the finished template."
}

variable "boot_iso_type" {
  type        = string
  default     = "ide"
  description = "Bus type for the installer ISO (ide, sata, or scsi)."
}

variable "autoinstall_delivery" {
  type        = string
  default     = "http"
  description = "How to deliver Ubuntu autoinstall: http (Packer HTTP server) or iso (cidata seed ISO)."
  validation {
    condition     = contains(["http", "iso"], var.autoinstall_delivery)
    error_message = "The autoinstall_delivery value must be http or iso."
  }
}

variable "http_bind_address" {
  type        = string
  default     = ""
  description = "Address Packer's HTTP server binds to. Set this to an IP the VM can reach when using autoinstall_delivery=http."
}

variable "http_interface" {
  type        = string
  default     = ""
  description = "Host interface Packer uses to choose HTTPIP. Alternative to http_bind_address."
}

variable "http_port_min" {
  type        = number
  default     = 8000
  description = "Minimum port for Packer's autoinstall HTTP server."
}

variable "http_port_max" {
  type        = number
  default     = 9000
  description = "Maximum port for Packer's autoinstall HTTP server."
}

variable "boot_wait" {
  type        = string
  default     = "10s"
  description = "Time to wait after power-on before sending the boot_command."
}

variable "boot_command" {
  type        = list(string)
  default     = []
  description = "Override the GRUB autoinstall boot_command. Empty uses the Ubuntu 26.04 default for the selected delivery method."
}

// Communicator
variable "ssh_username" {
  type        = string
  default     = "packer"
  description = "SSH user created by autoinstall. Must match http/user-data."
}

variable "ssh_password" {
  type        = string
  default     = "packer"
  sensitive   = true
  description = "SSH password for the autoinstall user during the build. Must match the hash in http/user-data."
}

variable "ssh_timeout" {
  type        = string
  default     = "60m"
  description = "How long Packer waits for SSH after autoinstall (OS install, not the later provisioners)."
}

variable "ssh_handshake_attempts" {
  type        = number
  default     = 100
  description = "SSH handshake attempts while waiting for the installer to finish."
}

variable "ssh_clear_authorized_keys" {
  type        = bool
  default     = true
  description = "Remove Packer's temporary authorized_keys from the image."
}

// Image generation (shared with the Azure Ubuntu path)
variable "helper_script_folder" {
  type    = string
  default = "/imagegeneration/helpers"
}

variable "image_folder" {
  type    = string
  default = "/imagegeneration"
}

variable "image_os" {
  type        = string
  default     = "ubuntu26"
  description = "Image OS key used by configure-environment.sh and locals on Azure. Keep ubuntu26 for Ubuntu 26.04."
}

variable "image_version" {
  type    = string
  default = "dev"
}

variable "imagedata_file" {
  type    = string
  default = "/imagegeneration/imagedata.json"
}

variable "installer_script_folder" {
  type    = string
  default = "/imagegeneration/installers"
}
