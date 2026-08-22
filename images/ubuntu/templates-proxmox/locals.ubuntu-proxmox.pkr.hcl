locals {
  default_boot_command_http = [
    "<esc><wait>",
    "e<wait>",
    "<down><down><down><end>",
    "<bs><bs><bs><bs><wait>",
    "autoinstall ds=nocloud-net\\;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/ ---<wait>",
    "<f10><wait>"
  ]

  default_boot_command_iso = [
    "<esc><wait>",
    "e<wait>",
    "<down><down><down><end>",
    "<bs><bs><bs><bs><wait>",
    "autoinstall ds=nocloud ---<wait>",
    "<f10><wait>"
  ]

  boot_command = length(var.boot_command) > 0 ? var.boot_command : (
    var.autoinstall_delivery == "iso" ? local.default_boot_command_iso : local.default_boot_command_http
  )

  template_description = var.template_description != "" ? var.template_description : "GitHub Actions runner image (Ubuntu 26.04) from runner-images. Clone this template and run /opt/post-generation scripts as the clone user."

  cloud_init_storage_pool = var.cloud_init_storage_pool != "" ? var.cloud_init_storage_pool : var.disk_storage_pool
}
