variable "deployment_provider" {
  description = "Target infrastructure provider."
  type        = string
  default     = "aws"

  validation {
    condition     = contains(["aws", "gcp", "bare_metal"], var.deployment_provider)
    error_message = "deployment_provider must be one of aws, gcp, or bare_metal."
  }
}

variable "node_type" {
  description = "Logical node role."
  type        = string

  validation {
    condition = contains(
      ["gpu_inference", "cpu_service", "edge_gateway", "monitoring"],
      var.node_type,
    )
    error_message = "node_type must be gpu_inference, cpu_service, edge_gateway, or monitoring."
  }
}

variable "name_prefix" {
  description = "Prefix used when naming instances."
  type        = string
}

variable "node_count" {
  description = "Number of nodes to provision for cloud providers."
  type        = number
  default     = 1
}

variable "assign_public_ip" {
  description = "Whether cloud instances should receive public IPs."
  type        = bool
  default     = false
}

variable "ssh_public_key" {
  description = "SSH public key material injected into instances where supported."
  type        = string
  default     = ""
}

variable "aws_ami_id" {
  description = "AMI used for non-GPU AWS instances."
  type        = string
  default     = "ami-xxxxxxxx"
}

variable "aws_gpu_ami_id" {
  description = "AMI used for GPU AWS instances. Falls back to aws_ami_id when unset."
  type        = string
  default     = ""
}

variable "aws_key_name" {
  description = "Optional AWS EC2 key pair name."
  type        = string
  default     = null
}

variable "aws_subnet_id" {
  description = "AWS subnet ID for the instances."
  type        = string
  default     = null
}

variable "aws_security_group_ids" {
  description = "AWS security groups attached to the instances."
  type        = list(string)
  default     = []
}

variable "aws_root_volume_size_gb" {
  description = "AWS root disk size in GiB."
  type        = number
  default     = 100
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

variable "gcp_zone" {
  description = "GCP zone."
  type        = string
  default     = "us-central1-a"
}

variable "gcp_subnetwork" {
  description = "GCP subnetwork self-link or name."
  type        = string
  default     = null
}

variable "gcp_image" {
  description = "Image used for non-GPU GCP instances."
  type        = string
  default     = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
}

variable "gcp_gpu_image" {
  description = "Image used for GPU GCP instances. Falls back to gcp_image when unset."
  type        = string
  default     = ""
}

variable "gcp_boot_disk_size_gb" {
  description = "GCP boot disk size in GiB."
  type        = number
  default     = 100
}

variable "gcp_service_account_email" {
  description = "Optional GCP service account email."
  type        = string
  default     = null
}

variable "gcp_tags" {
  description = "Additional GCP network tags."
  type        = list(string)
  default     = []
}

variable "gcp_labels" {
  description = "Additional GCP labels."
  type        = map(string)
  default     = {}
}

variable "gcp_gpu_accelerator_type" {
  description = "GPU accelerator type for gpu_inference nodes."
  type        = string
  default     = "nvidia-tesla-t4"
}

variable "bare_metal_hostnames" {
  description = "Existing bare-metal hostnames when deployment_provider=bare_metal."
  type        = list(string)
  default     = []
}

variable "bare_metal_private_ips" {
  description = "Existing bare-metal private IPs when deployment_provider=bare_metal."
  type        = list(string)
  default     = []
}
