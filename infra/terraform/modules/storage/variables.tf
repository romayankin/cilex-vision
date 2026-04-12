variable "deployment_provider" {
  description = "Target infrastructure provider."
  type        = string
  default     = "aws"

  validation {
    condition     = contains(["aws", "gcp", "bare_metal"], var.deployment_provider)
    error_message = "deployment_provider must be one of aws, gcp, or bare_metal."
  }
}

variable "name_prefix" {
  description = "Prefix used for storage resources."
  type        = string
  default     = "cilex"
}

variable "availability_zone" {
  description = "AWS availability zone for block volumes."
  type        = string
  default     = "us-east-1a"
}

variable "gcp_zone" {
  description = "GCP zone for persistent disks."
  type        = string
  default     = "us-central1-a"
}

variable "timescaledb_size_gb" {
  description = "TimescaleDB volume size in GiB."
  type        = number
  default     = 500
}

variable "timescaledb_iops" {
  description = "TimescaleDB requested IOPS."
  type        = number
  default     = 3000
}

variable "timescaledb_volume_type" {
  description = "TimescaleDB volume type."
  type        = string
  default     = "gp3"
}

variable "minio_size_gb" {
  description = "MinIO volume size in GiB."
  type        = number
  default     = 1000
}

variable "minio_iops" {
  description = "MinIO requested IOPS."
  type        = number
  default     = 3000
}

variable "minio_volume_type" {
  description = "MinIO volume type."
  type        = string
  default     = "gp3"
}

variable "faiss_size_gb" {
  description = "Per-GPU-node FAISS spool volume size in GiB."
  type        = number
  default     = 100
}

variable "faiss_iops" {
  description = "FAISS spool requested IOPS."
  type        = number
  default     = 6000
}

variable "faiss_volume_type" {
  description = "FAISS spool volume type."
  type        = string
  default     = "io2"
}

variable "kafka_size_gb" {
  description = "Per-broker Kafka log volume size in GiB."
  type        = number
  default     = 200
}

variable "kafka_iops" {
  description = "Kafka log requested IOPS."
  type        = number
  default     = 3000
}

variable "kafka_volume_type" {
  description = "Kafka volume type."
  type        = string
  default     = "gp3"
}

variable "kafka_broker_count" {
  description = "Number of Kafka broker data volumes."
  type        = number
  default     = 3
}

variable "gpu_node_count" {
  description = "Number of FAISS spool volumes, one per GPU node."
  type        = number
  default     = 2
}

variable "aws_tags" {
  description = "Additional AWS tags."
  type        = map(string)
  default     = {}
}

variable "gcp_labels" {
  description = "Additional GCP labels."
  type        = map(string)
  default     = {}
}
