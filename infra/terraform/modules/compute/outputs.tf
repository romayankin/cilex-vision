output "hostnames" {
  description = "Provisioned hostnames."
  value = (
    var.provider == "aws"
    ? aws_instance.nodes[*].tags.Name
    : var.provider == "gcp"
    ? google_compute_instance.nodes[*].name
    : var.bare_metal_hostnames
  )
}

output "private_ips" {
  description = "Provisioned private IP addresses."
  value = (
    var.provider == "aws"
    ? aws_instance.nodes[*].private_ip
    : var.provider == "gcp"
    ? google_compute_instance.nodes[*].network_interface[0].network_ip
    : var.bare_metal_private_ips
  )
}

output "public_ips" {
  description = "Provisioned public IP addresses when requested."
  value = (
    var.provider == "aws"
    ? aws_instance.nodes[*].public_ip
    : var.provider == "gcp"
    ? flatten(google_compute_instance.nodes[*].network_interface[0].access_config[*].nat_ip)
    : []
  )
}

output "ssh_keys" {
  description = "SSH key references or injected public keys."
  value = compact(
    var.provider == "aws"
    ? [var.aws_key_name]
    : [var.ssh_public_key]
  )
}
