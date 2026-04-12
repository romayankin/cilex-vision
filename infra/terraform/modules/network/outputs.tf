output "network_id" {
  description = "Provisioned network identifier."
  value = (
    var.deployment_provider == "aws"
    ? aws_vpc.this[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_network.this[0].id
    : "bare-metal-network"
  )
}

output "core_subnet_id" {
  description = "Core subnet identifier."
  value = (
    var.deployment_provider == "aws"
    ? aws_subnet.core[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_subnetwork.core[0].self_link
    : "bare-metal-core"
  )
}

output "edge_subnet_id" {
  description = "Edge subnet identifier."
  value = (
    var.deployment_provider == "aws"
    ? aws_subnet.edge[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_subnetwork.edge[0].self_link
    : "bare-metal-edge"
  )
}

output "camera_subnet_id" {
  description = "Camera subnet identifier."
  value = (
    var.deployment_provider == "aws"
    ? aws_subnet.camera[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_subnetwork.camera[0].self_link
    : "bare-metal-camera"
  )
}

output "core_internal_id" {
  description = "Core internal security control ID or name."
  value = (
    var.deployment_provider == "aws"
    ? aws_security_group.core_internal[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_firewall.core_internal[0].name
    : "bare-metal-core-internal"
  )
}

output "edge_to_core_id" {
  description = "Edge-to-core security control ID or name."
  value = (
    var.deployment_provider == "aws"
    ? aws_security_group.edge_to_core[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_firewall.edge_to_core[0].name
    : "bare-metal-edge-to-core"
  )
}

output "camera_to_edge_id" {
  description = "Camera-to-edge security control ID or name."
  value = (
    var.deployment_provider == "aws"
    ? aws_security_group.camera_to_edge[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_firewall.camera_to_edge[0].name
    : "bare-metal-camera-to-edge"
  )
}

output "monitoring_id" {
  description = "Monitoring security control ID or name."
  value = (
    var.deployment_provider == "aws"
    ? aws_security_group.monitoring[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_firewall.monitoring[0].name
    : "bare-metal-monitoring"
  )
}

output "public_api_id" {
  description = "Public API security control ID or name."
  value = (
    var.deployment_provider == "aws"
    ? aws_security_group.public_api[0].id
    : var.deployment_provider == "gcp"
    ? google_compute_firewall.public_api[0].name
    : "bare-metal-public-api"
  )
}
