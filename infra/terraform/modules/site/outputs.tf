output "site_id" {
  description = "Short site identifier."
  value       = var.site_id
}

output "site_name" {
  description = "Human-readable site name."
  value       = var.site_name
}

output "edge_hostnames" {
  description = "Provisioned site edge gateway hostnames."
  value       = module.edge_gateways.hostnames
}

output "edge_private_ips" {
  description = "Provisioned site edge gateway private IPs."
  value       = module.edge_gateways.private_ips
}

output "edge_public_ips" {
  description = "Provisioned site edge gateway public IPs."
  value       = module.edge_gateways.public_ips
}

output "site_vpn_endpoint" {
  description = "Primary WireGuard endpoint for the site."
  value       = local.site_vpn_endpoint
}

output "site_storage_endpoint" {
  description = "Primary site-local MinIO endpoint published by the edge role."
  value       = local.site_storage_endpoint
}

output "site_storage_volume_ids" {
  description = "Provisioned site-local storage volume IDs or placeholders."
  value = (
    var.deployment_provider == "aws"
    ? aws_ebs_volume.site_storage[*].id
    : var.deployment_provider == "gcp"
    ? google_compute_disk.site_storage[*].id
    : null_resource.site_storage[*].id
  )
}

output "network_id" {
  description = "Site network identifier."
  value       = module.network.network_id
}

output "edge_subnet_id" {
  description = "Site edge subnet identifier."
  value       = module.network.edge_subnet_id
}

output "camera_subnet_id" {
  description = "Site camera subnet identifier."
  value       = module.network.camera_subnet_id
}
