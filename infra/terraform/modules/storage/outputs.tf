output "timescaledb_volume_ids" {
  description = "TimescaleDB volume IDs or placeholders."
  value = (
    var.deployment_provider == "aws"
    ? aws_ebs_volume.timescaledb[*].id
    : var.deployment_provider == "gcp"
    ? google_compute_disk.timescaledb[*].id
    : null_resource.timescaledb[*].id
  )
}

output "minio_volume_ids" {
  description = "MinIO volume IDs or placeholders."
  value = (
    var.deployment_provider == "aws"
    ? aws_ebs_volume.minio[*].id
    : var.deployment_provider == "gcp"
    ? google_compute_disk.minio[*].id
    : null_resource.minio[*].id
  )
}

output "faiss_volume_ids" {
  description = "FAISS spool volume IDs or placeholders."
  value = (
    var.deployment_provider == "aws"
    ? aws_ebs_volume.faiss[*].id
    : var.deployment_provider == "gcp"
    ? google_compute_disk.faiss[*].id
    : null_resource.faiss[*].id
  )
}

output "kafka_volume_ids" {
  description = "Kafka broker volume IDs or placeholders."
  value = (
    var.deployment_provider == "aws"
    ? aws_ebs_volume.kafka[*].id
    : var.deployment_provider == "gcp"
    ? google_compute_disk.kafka[*].id
    : null_resource.kafka[*].id
  )
}
