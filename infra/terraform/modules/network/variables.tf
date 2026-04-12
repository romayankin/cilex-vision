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
  description = "Prefix used for network resources."
  type        = string
  default     = "cilex"
}

variable "vpc_cidr" {
  description = "Top-level VPC CIDR."
  type        = string
  default     = "10.0.0.0/8"
}

variable "core_subnet_cidr" {
  description = "Core subnet CIDR."
  type        = string
  default     = "10.43.0.0/16"
}

variable "edge_subnet_cidr" {
  description = "Edge subnet CIDR."
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

variable "gcp_labels" {
  description = "Additional GCP labels."
  type        = map(string)
  default     = {}
}
