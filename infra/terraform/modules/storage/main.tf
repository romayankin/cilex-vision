resource "aws_ebs_volume" "timescaledb" {
  count = var.deployment_provider == "aws" ? 1 : 0

  availability_zone = var.availability_zone
  size              = var.timescaledb_size_gb
  iops              = var.timescaledb_iops
  type              = var.timescaledb_volume_type

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-timescaledb-data" })
}

resource "aws_ebs_volume" "minio" {
  count = var.deployment_provider == "aws" ? 1 : 0

  availability_zone = var.availability_zone
  size              = var.minio_size_gb
  iops              = var.minio_iops
  type              = var.minio_volume_type

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-minio-data" })
}

resource "aws_ebs_volume" "faiss" {
  count = var.deployment_provider == "aws" ? var.gpu_node_count : 0

  availability_zone = var.availability_zone
  size              = var.faiss_size_gb
  iops              = var.faiss_iops
  type              = var.faiss_volume_type

  tags = merge(
    var.aws_tags,
    { Name = format("%s-faiss-%02d", var.name_prefix, count.index + 1) },
  )
}

resource "aws_ebs_volume" "kafka" {
  count = var.deployment_provider == "aws" ? var.kafka_broker_count : 0

  availability_zone = var.availability_zone
  size              = var.kafka_size_gb
  iops              = var.kafka_iops
  type              = var.kafka_volume_type

  tags = merge(
    var.aws_tags,
    { Name = format("%s-kafka-%02d", var.name_prefix, count.index + 1) },
  )
}

resource "google_compute_disk" "timescaledb" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  name   = "${var.name_prefix}-timescaledb-data"
  zone   = var.gcp_zone
  size   = var.timescaledb_size_gb
  type   = var.timescaledb_volume_type == "io2" ? "pd-ssd" : "pd-balanced"
  labels = merge(var.gcp_labels, { role = "timescaledb" })
}

resource "google_compute_disk" "minio" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  name   = "${var.name_prefix}-minio-data"
  zone   = var.gcp_zone
  size   = var.minio_size_gb
  type   = var.minio_volume_type == "io2" ? "pd-ssd" : "pd-balanced"
  labels = merge(var.gcp_labels, { role = "minio" })
}

resource "google_compute_disk" "faiss" {
  count = var.deployment_provider == "gcp" ? var.gpu_node_count : 0

  name   = format("%s-faiss-%02d", var.name_prefix, count.index + 1)
  zone   = var.gcp_zone
  size   = var.faiss_size_gb
  type   = var.faiss_volume_type == "io2" ? "pd-ssd" : "pd-balanced"
  labels = merge(var.gcp_labels, { role = "faiss" })
}

resource "google_compute_disk" "kafka" {
  count = var.deployment_provider == "gcp" ? var.kafka_broker_count : 0

  name   = format("%s-kafka-%02d", var.name_prefix, count.index + 1)
  zone   = var.gcp_zone
  size   = var.kafka_size_gb
  type   = var.kafka_volume_type == "io2" ? "pd-ssd" : "pd-balanced"
  labels = merge(var.gcp_labels, { role = "kafka" })
}

resource "null_resource" "timescaledb" {
  count = var.deployment_provider == "bare_metal" ? 1 : 0

  triggers = {
    name = "${var.name_prefix}-timescaledb-data"
    size = tostring(var.timescaledb_size_gb)
    iops = tostring(var.timescaledb_iops)
  }
}

resource "null_resource" "minio" {
  count = var.deployment_provider == "bare_metal" ? 1 : 0

  triggers = {
    name = "${var.name_prefix}-minio-data"
    size = tostring(var.minio_size_gb)
    iops = tostring(var.minio_iops)
  }
}

resource "null_resource" "faiss" {
  count = var.deployment_provider == "bare_metal" ? var.gpu_node_count : 0

  triggers = {
    name = format("%s-faiss-%02d", var.name_prefix, count.index + 1)
    size = tostring(var.faiss_size_gb)
    iops = tostring(var.faiss_iops)
  }
}

resource "null_resource" "kafka" {
  count = var.deployment_provider == "bare_metal" ? var.kafka_broker_count : 0

  triggers = {
    name = format("%s-kafka-%02d", var.name_prefix, count.index + 1)
    size = tostring(var.kafka_size_gb)
    iops = tostring(var.kafka_iops)
  }
}
