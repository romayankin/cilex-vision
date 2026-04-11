output "network_id" {
  description = "Central network identifier."
  value       = module.network.network_id
}

output "core_subnet_id" {
  description = "Central core subnet identifier."
  value       = module.network.core_subnet_id
}

output "core_subnet_cidr" {
  description = "Central core subnet CIDR used for site VPN allowlists."
  value       = var.core_subnet_cidr
}

output "edge_subnet_id" {
  description = "Central edge transit subnet identifier."
  value       = module.network.edge_subnet_id
}

output "kafka_hostnames" {
  description = "Kafka hostnames."
  value       = module.kafka.hostnames
}

output "kafka_private_ips" {
  description = "Kafka private IPs."
  value       = module.kafka.private_ips
}

output "timescaledb_hostnames" {
  description = "TimescaleDB hostnames."
  value       = module.timescaledb.hostnames
}

output "minio_hostnames" {
  description = "Central MinIO hostnames."
  value       = module.minio.hostnames
}

output "gpu_hostnames" {
  description = "GPU inference hostnames."
  value       = module.gpu_pool.hostnames
}

output "monitoring_hostnames" {
  description = "Monitoring hostnames."
  value       = module.monitoring.hostnames
}

output "service_hostnames" {
  description = "Central service hostnames."
  value       = module.services.hostnames
}

output "timescaledb_volume_ids" {
  description = "TimescaleDB storage volume IDs."
  value       = module.storage.timescaledb_volume_ids
}

output "minio_volume_ids" {
  description = "MinIO storage volume IDs."
  value       = module.storage.minio_volume_ids
}

output "faiss_volume_ids" {
  description = "FAISS spool volume IDs."
  value       = module.storage.faiss_volume_ids
}

output "kafka_volume_ids" {
  description = "Kafka volume IDs."
  value       = module.storage.kafka_volume_ids
}

output "central_vpn_endpoint" {
  description = "Primary central WireGuard endpoint placeholder."
  value       = local.central_vpn_host != "" ? "udp://${local.central_vpn_host}:51820" : ""
}
