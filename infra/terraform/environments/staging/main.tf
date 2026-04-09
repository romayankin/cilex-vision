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

variable "site_prefix" {
  description = "Name prefix for staging resources."
  type        = string
  default     = "cilex-staging"
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
  description = "Base AMI for staging non-GPU nodes."
  type        = string
  default     = "ami-xxxxxxxx"
}

variable "aws_gpu_ami_id" {
  description = "GPU-enabled AMI for Triton nodes."
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

variable "bare_metal_hosts" {
  description = "Per-role bare-metal hostnames and IPs when deployment_provider=bare_metal."
  type = map(object({
    hostnames   = list(string)
    private_ips = list(string)
  }))
  default = {}
}

locals {
  counts = {
    kafka         = 1
    timescaledb   = 1
    minio         = 1
    triton        = 1
    monitoring    = 1
    services      = 1
    edge_gateways = 1
    nats          = 1
    mlflow        = 1
  }

  common_aws_tags = {
    Environment = "staging"
    Project     = "cilex-vision"
  }
}

provider "aws" {
  region = var.aws_region
}

provider "google" {
  project = var.gcp_project
  region  = var.gcp_region
  zone    = var.gcp_zone
}

module "network" {
  source = "../../modules/network"

  provider           = var.deployment_provider
  name_prefix        = var.site_prefix
  vpc_cidr           = "10.0.0.0/8"
  core_subnet_cidr   = "10.43.0.0/16"
  edge_subnet_cidr   = "10.42.0.0/16"
  camera_subnet_cidr = "192.168.0.0/16"
  public_https_cidrs = var.public_https_cidrs
  aws_tags           = local.common_aws_tags
  gcp_project        = var.gcp_project
  gcp_region         = var.gcp_region
}

module "storage" {
  source = "../../modules/storage"

  provider           = var.deployment_provider
  name_prefix        = var.site_prefix
  availability_zone  = var.aws_availability_zone
  gcp_zone           = var.gcp_zone
  kafka_broker_count = local.counts.kafka
  gpu_node_count     = local.counts.triton
  aws_tags           = local.common_aws_tags
}

module "kafka" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.site_prefix}-kafka"
  node_count             = local.counts.kafka
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = [module.network.core_internal_id, module.network.edge_to_core_id, module.network.monitoring_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["kafka"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["kafka"].private_ips, [])
}

module "timescaledb" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.site_prefix}-timescaledb"
  node_count             = local.counts.timescaledb
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = [module.network.core_internal_id, module.network.monitoring_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["timescaledb"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["timescaledb"].private_ips, [])
}

module "minio" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.site_prefix}-minio"
  node_count             = local.counts.minio
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = [module.network.core_internal_id, module.network.monitoring_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["minio"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["minio"].private_ips, [])
}

module "triton" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "gpu_inference"
  name_prefix            = "${var.site_prefix}-triton"
  node_count             = local.counts.triton
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_gpu_ami_id         = var.aws_gpu_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = [module.network.core_internal_id, module.network.monitoring_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["triton"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["triton"].private_ips, [])
}

module "monitoring" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "monitoring"
  name_prefix            = "${var.site_prefix}-monitoring"
  node_count             = local.counts.monitoring
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = [module.network.core_internal_id, module.network.monitoring_id, module.network.public_api_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target", "api-gateway"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["monitoring"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["monitoring"].private_ips, [])
}

module "services" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.site_prefix}-services"
  node_count             = local.counts.services
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = [module.network.core_internal_id, module.network.monitoring_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["services"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["services"].private_ips, [])
}

module "edge_gateways" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "edge_gateway"
  name_prefix            = "${var.site_prefix}-edge"
  node_count             = local.counts.edge_gateways
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.edge_subnet_id
  aws_security_group_ids = [module.network.camera_to_edge_id, module.network.monitoring_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.edge_subnet_id
  gcp_tags               = ["edge-gateway", "monitoring-target"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["edge_gateways"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["edge_gateways"].private_ips, [])
}

module "nats" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "edge_gateway"
  name_prefix            = "${var.site_prefix}-nats"
  node_count             = local.counts.nats
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.edge_subnet_id
  aws_security_group_ids = [module.network.monitoring_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.edge_subnet_id
  gcp_tags               = ["edge-gateway", "monitoring-target"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["nats"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["nats"].private_ips, [])
}

module "mlflow" {
  source = "../../modules/compute"

  provider               = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.site_prefix}-mlflow"
  node_count             = local.counts.mlflow
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = [module.network.core_internal_id, module.network.monitoring_id]
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  bare_metal_hostnames   = try(var.bare_metal_hosts["mlflow"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["mlflow"].private_ips, [])
}

output "node_inventory" {
  description = "Computed hostname and private IP inventory seed."
  value = {
    kafka = {
      hostnames   = module.kafka.hostnames
      private_ips = module.kafka.private_ips
    }
    timescaledb = {
      hostnames   = module.timescaledb.hostnames
      private_ips = module.timescaledb.private_ips
    }
    minio = {
      hostnames   = module.minio.hostnames
      private_ips = module.minio.private_ips
    }
    triton = {
      hostnames   = module.triton.hostnames
      private_ips = module.triton.private_ips
    }
    monitoring = {
      hostnames   = module.monitoring.hostnames
      private_ips = module.monitoring.private_ips
    }
    services = {
      hostnames   = module.services.hostnames
      private_ips = module.services.private_ips
    }
    edge_gateways = {
      hostnames   = module.edge_gateways.hostnames
      private_ips = module.edge_gateways.private_ips
    }
    nats = {
      hostnames   = module.nats.hostnames
      private_ips = module.nats.private_ips
    }
    mlflow = {
      hostnames   = module.mlflow.hostnames
      private_ips = module.mlflow.private_ips
    }
  }
}
