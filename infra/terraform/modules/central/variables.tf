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
  description = "Prefix used for central resources."
  type        = string
  default     = "cilex-central"
}

variable "site_count" {
  description = "Number of connected sites served by this central deployment."
  type        = number
  default     = 1
}

variable "gpu_node_count" {
  description = "Number of GPU inference nodes."
  type        = number
  default     = 2
}

variable "kafka_broker_count" {
  description = "Number of Kafka brokers."
  type        = number
  default     = 3
}

variable "monitoring_node_count" {
  description = "Number of monitoring nodes."
  type        = number
  default     = 1
}

variable "timescaledb_node_count" {
  description = "Number of TimescaleDB nodes."
  type        = number
  default     = 1
}

variable "minio_node_count" {
  description = "Number of central MinIO nodes."
  type        = number
  default     = 1
}

variable "service_node_count" {
  description = "Number of central service nodes."
  type        = number
  default     = 2
}

variable "assign_public_ip" {
  description = "Whether public HTTPS and monitoring nodes should receive public IPs."
  type        = bool
  default     = true
}

variable "ssh_public_key" {
  description = "SSH public key material injected into instances where supported."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "Central VPC CIDR."
  type        = string
  default     = "10.0.0.0/8"
}

variable "core_subnet_cidr" {
  description = "Central core subnet CIDR."
  type        = string
  default     = "10.43.0.0/16"
}

variable "edge_subnet_cidr" {
  description = "Central edge transit subnet CIDR."
  type        = string
  default     = "10.42.0.0/16"
}

variable "camera_subnet_cidr" {
  description = "Camera VLAN CIDR."
  type        = string
  default     = "192.168.0.0/16"
}

variable "public_https_cidrs" {
  description = "CIDRs allowed to reach public HTTPS entrypoints."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "aws_region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "aws_availability_zone" {
  description = "AWS availability zone for compute and storage."
  type        = string
  default     = "us-east-1a"
}

variable "aws_linux_ami_id" {
  description = "Base AMI for non-GPU nodes."
  type        = string
  default     = "ami-xxxxxxxx"
}

variable "aws_gpu_ami_id" {
  description = "GPU-enabled AMI for inference nodes."
  type        = string
  default     = "ami-yyyyyyyy"
}

variable "aws_key_name" {
  description = "Optional AWS EC2 key pair name."
  type        = string
  default     = null
}

variable "aws_tags" {
  description = "Additional AWS tags."
  type        = map(string)
  default     = {}
}

variable "gcp_project" {
  description = "GCP project ID."
  type        = string
  default     = ""
}

variable "gcp_region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "gcp_zone" {
  description = "GCP zone."
  type        = string
  default     = "us-central1-a"
}

variable "gcp_labels" {
  description = "Additional GCP labels."
  type        = map(string)
  default     = {}
}

variable "bare_metal_hosts" {
  description = "Per-role bare-metal hostnames and IPs when deployment_provider=bare_metal."
  type = map(object({
    hostnames   = list(string)
    private_ips = list(string)
  }))
  default = {}
}
