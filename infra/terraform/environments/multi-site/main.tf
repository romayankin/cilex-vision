terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }

  backend "s3" {}
}

variable "deployment_provider" {
  description = "aws, gcp, or bare_metal."
  type        = string
  default     = "aws"
}

variable "deployment_name" {
  description = "Prefix used for the multi-site deployment."
  type        = string
  default     = "cilex-multi"
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
  description = "AWS EC2 key pair name."
  type        = string
  default     = null
}

variable "ssh_public_key" {
  description = "SSH public key for cloud metadata or bare-metal inventories."
  type        = string
  default     = ""
}

variable "public_https_cidrs" {
  description = "CIDRs allowed to reach public HTTPS entrypoints."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "central_gpu_node_count" {
  description = "Number of central GPU inference nodes."
  type        = number
  default     = 2
}

variable "central_kafka_broker_count" {
  description = "Number of central Kafka brokers."
  type        = number
  default     = 3
}

variable "bare_metal_hosts" {
  description = "Per-role bare-metal hostnames and IPs when deployment_provider=bare_metal."
  type = map(object({
    hostnames   = list(string)
    private_ips = list(string)
  }))
  default = {}
}

variable "sites" {
  description = "Multi-site edge definitions keyed by site slug."
  type = map(object({
    site_name              = string
    edge_count             = number
    camera_count           = number
    vpc_cidr               = string
    core_subnet_cidr       = string
    edge_subnet_cidr       = string
    camera_subnet_cidr     = string
    assign_public_ip       = optional(bool, true)
    storage_size_gb        = optional(number, 250)
    storage_iops           = optional(number, 3000)
    bare_metal_hostnames   = optional(list(string), [])
    bare_metal_private_ips = optional(list(string), [])
  }))
  default = {
    alpha = {
      site_name          = "Alpha Site"
      edge_count         = 1
      camera_count       = 10
      vpc_cidr           = "10.44.0.0/16"
      core_subnet_cidr   = "10.44.10.0/24"
      edge_subnet_cidr   = "10.44.20.0/24"
      camera_subnet_cidr = "192.168.10.0/24"
      assign_public_ip   = true
      storage_size_gb    = 250
      storage_iops       = 3000
    }
    beta = {
      site_name          = "Beta Site"
      edge_count         = 1
      camera_count       = 8
      vpc_cidr           = "10.45.0.0/16"
      core_subnet_cidr   = "10.45.10.0/24"
      edge_subnet_cidr   = "10.45.20.0/24"
      camera_subnet_cidr = "192.168.20.0/24"
      assign_public_ip   = true
      storage_size_gb    = 250
      storage_iops       = 3000
    }
  }
}

locals {
  workspace_prefix = "${var.deployment_name}-${terraform.workspace}"
}

provider "aws" {
  region = var.aws_region
}

provider "google" {
  project = var.gcp_project
  region  = var.gcp_region
  zone    = var.gcp_zone
}

module "central" {
  source = "../../modules/central"

  deployment_provider   = var.deployment_provider
  name_prefix           = "${local.workspace_prefix}-central"
  site_count            = length(var.sites)
  gpu_node_count        = var.central_gpu_node_count
  kafka_broker_count    = var.central_kafka_broker_count
  ssh_public_key        = var.ssh_public_key
  public_https_cidrs    = var.public_https_cidrs
  aws_availability_zone = var.aws_availability_zone
  aws_linux_ami_id      = var.aws_linux_ami_id
  aws_gpu_ami_id        = var.aws_gpu_ami_id
  aws_key_name          = var.aws_key_name
  gcp_project           = var.gcp_project
  gcp_region            = var.gcp_region
  gcp_zone              = var.gcp_zone
  bare_metal_hosts      = var.bare_metal_hosts
}

module "sites" {
  source   = "../../modules/site"
  for_each = var.sites

  deployment_provider    = var.deployment_provider
  site_id                = each.key
  site_name              = each.value.site_name
  name_prefix            = "${local.workspace_prefix}-site"
  edge_count             = each.value.edge_count
  camera_count           = each.value.camera_count
  assign_public_ip       = each.value.assign_public_ip
  ssh_public_key         = var.ssh_public_key
  vpc_cidr               = each.value.vpc_cidr
  site_core_subnet_cidr  = each.value.core_subnet_cidr
  edge_subnet_cidr       = each.value.edge_subnet_cidr
  camera_subnet_cidr     = each.value.camera_subnet_cidr
  central_vpn_cidrs      = [module.central.core_subnet_cidr]
  site_storage_size_gb   = each.value.storage_size_gb
  site_storage_iops      = each.value.storage_iops
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_availability_zone  = var.aws_availability_zone
  gcp_project            = var.gcp_project
  gcp_region             = var.gcp_region
  gcp_zone               = var.gcp_zone
  bare_metal_hostnames   = each.value.bare_metal_hostnames
  bare_metal_private_ips = each.value.bare_metal_private_ips
}

resource "null_resource" "site_wireguard_peering" {
  for_each = module.sites

  triggers = {
    site_id              = each.value.site_id
    central_vpn_endpoint = module.central.central_vpn_endpoint
    site_vpn_endpoint    = each.value.site_vpn_endpoint
    workspace            = terraform.workspace
  }
}

output "central_summary" {
  description = "Central multi-site infrastructure summary."
  value = {
    network_id           = module.central.network_id
    kafka_hostnames      = module.central.kafka_hostnames
    timescaledb_hosts    = module.central.timescaledb_hostnames
    minio_hosts          = module.central.minio_hostnames
    gpu_hosts            = module.central.gpu_hostnames
    monitoring_hosts     = module.central.monitoring_hostnames
    service_hosts        = module.central.service_hostnames
    central_vpn_endpoint = module.central.central_vpn_endpoint
  }
}

output "site_summaries" {
  description = "Per-site edge, storage, and VPN endpoints."
  value = {
    for site_id, site in module.sites : site_id => {
      site_name             = site.site_name
      edge_hostnames        = site.edge_hostnames
      edge_private_ips      = site.edge_private_ips
      edge_public_ips       = site.edge_public_ips
      site_vpn_endpoint     = site.site_vpn_endpoint
      site_storage_endpoint = site.site_storage_endpoint
      network_id            = site.network_id
    }
  }
}
