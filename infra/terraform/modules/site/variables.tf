variable "provider" {
  description = "Target infrastructure provider."
  type        = string
  default     = "aws"

  validation {
    condition     = contains(["aws", "gcp", "bare_metal"], var.provider)
    error_message = "provider must be one of aws, gcp, or bare_metal."
  }
}

variable "site_id" {
  description = "Short site identifier used in hostnames and tags."
  type        = string
}

variable "site_name" {
  description = "Human-readable site name."
  type        = string
}

variable "name_prefix" {
  description = "Prefix used for provisioned resources."
  type        = string
  default     = "cilex"
}

variable "edge_count" {
  description = "Number of edge gateway nodes for the site."
  type        = number
  default     = 1
}

variable "camera_count" {
  description = "Expected camera count at the site."
  type        = number
  default     = 1
}

variable "assign_public_ip" {
  description = "Whether edge gateways should receive public IPs."
  type        = bool
  default     = true
}

variable "ssh_public_key" {
  description = "SSH public key material injected into instances where supported."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "Site-local top-level VPC CIDR."
  type        = string
}

variable "edge_subnet_cidr" {
  description = "Site edge subnet CIDR."
  type        = string
}

variable "camera_subnet_cidr" {
  description = "Site camera subnet CIDR."
  type        = string
}

variable "site_core_subnet_cidr" {
  description = "Site control subnet CIDR used for monitoring and local services."
  type        = string
}

variable "central_vpn_cidrs" {
  description = "CIDRs allowed to initiate the site VPN/WireGuard endpoint."
  type        = list(string)
  default     = ["10.43.0.0/16"]
}

variable "wireguard_port" {
  description = "UDP port used for the site-to-central WireGuard tunnel."
  type        = number
  default     = 51820
}

variable "site_storage_size_gb" {
  description = "Per-edge-node local MinIO/frame-buffer volume size in GiB."
  type        = number
  default     = 250
}

variable "site_storage_iops" {
  description = "Per-edge-node local storage requested IOPS."
  type        = number
  default     = 3000
}

variable "site_storage_volume_type" {
  description = "Per-edge-node local storage volume type."
  type        = string
  default     = "gp3"
}

variable "site_storage_port" {
  description = "Local MinIO API port exposed by the edge role."
  type        = number
  default     = 9000
}

variable "aws_ami_id" {
  description = "AMI used for edge nodes."
  type        = string
  default     = "ami-xxxxxxxx"
}

variable "aws_key_name" {
  description = "Optional AWS EC2 key pair name."
  type        = string
  default     = null
}

variable "aws_availability_zone" {
  description = "AWS availability zone for compute and storage."
  type        = string
  default     = "us-east-1a"
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

variable "gcp_image" {
  description = "Image used for edge nodes on GCP."
  type        = string
  default     = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
}

variable "gcp_service_account_email" {
  description = "Optional GCP service account email."
  type        = string
  default     = null
}

variable "gcp_labels" {
  description = "Additional GCP labels."
  type        = map(string)
  default     = {}
}

variable "gcp_tags" {
  description = "Additional GCP network tags."
  type        = list(string)
  default     = []
}

variable "public_https_cidrs" {
  description = "CIDRs allowed to reach public HTTPS endpoints in the site network."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "bare_metal_hostnames" {
  description = "Existing bare-metal hostnames when provider=bare_metal."
  type        = list(string)
  default     = []
}

variable "bare_metal_private_ips" {
  description = "Existing bare-metal private IPs when provider=bare_metal."
  type        = list(string)
  default     = []
}
