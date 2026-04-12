locals {
  common_aws_tags = merge(
    var.aws_tags,
    {
      ManagedBy = "terraform"
      Layer     = "central"
      SiteCount = tostring(var.site_count)
    },
  )

  common_gcp_labels = merge(
    var.gcp_labels,
    {
      managed_by = "terraform"
      layer      = "central"
      site_count = tostring(var.site_count)
    },
  )
}

module "network" {
  source = "../network"

  deployment_provider = var.deployment_provider
  name_prefix         = var.name_prefix
  vpc_cidr            = var.vpc_cidr
  core_subnet_cidr    = var.core_subnet_cidr
  edge_subnet_cidr    = var.edge_subnet_cidr
  camera_subnet_cidr  = var.camera_subnet_cidr
  public_https_cidrs  = var.public_https_cidrs
  aws_tags            = local.common_aws_tags
  gcp_project         = var.gcp_project
  gcp_region          = var.gcp_region
  gcp_labels          = local.common_gcp_labels
}

module "storage" {
  source = "../storage"

  deployment_provider = var.deployment_provider
  name_prefix         = var.name_prefix
  availability_zone   = var.aws_availability_zone
  gcp_zone            = var.gcp_zone
  kafka_broker_count  = var.kafka_broker_count
  gpu_node_count      = var.gpu_node_count
  aws_tags            = local.common_aws_tags
  gcp_labels          = local.common_gcp_labels
}

module "kafka" {
  source = "../compute"

  deployment_provider    = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.name_prefix}-kafka"
  node_count             = var.kafka_broker_count
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = compact([module.network.core_internal_id, module.network.edge_to_core_id, module.network.monitoring_id])
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  gcp_labels             = local.common_gcp_labels
  bare_metal_hostnames   = try(var.bare_metal_hosts["kafka"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["kafka"].private_ips, [])
}

module "timescaledb" {
  source = "../compute"

  deployment_provider    = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.name_prefix}-timescaledb"
  node_count             = var.timescaledb_node_count
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = compact([module.network.core_internal_id, module.network.monitoring_id])
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  gcp_labels             = local.common_gcp_labels
  bare_metal_hostnames   = try(var.bare_metal_hosts["timescaledb"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["timescaledb"].private_ips, [])
}

module "minio" {
  source = "../compute"

  deployment_provider    = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.name_prefix}-minio"
  node_count             = var.minio_node_count
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = compact([module.network.core_internal_id, module.network.monitoring_id])
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  gcp_labels             = local.common_gcp_labels
  bare_metal_hostnames   = try(var.bare_metal_hosts["minio"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["minio"].private_ips, [])
}

module "gpu_pool" {
  source = "../compute"

  deployment_provider    = var.deployment_provider
  node_type              = "gpu_inference"
  name_prefix            = "${var.name_prefix}-triton"
  node_count             = var.gpu_node_count
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_gpu_ami_id         = var.aws_gpu_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = compact([module.network.core_internal_id, module.network.monitoring_id])
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  gcp_labels             = local.common_gcp_labels
  bare_metal_hostnames   = try(var.bare_metal_hosts["triton"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["triton"].private_ips, [])
}

module "monitoring" {
  source = "../compute"

  deployment_provider    = var.deployment_provider
  node_type              = "monitoring"
  name_prefix            = "${var.name_prefix}-monitoring"
  node_count             = var.monitoring_node_count
  assign_public_ip       = var.assign_public_ip
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = compact([module.network.core_internal_id, module.network.monitoring_id, module.network.public_api_id])
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target", "api-gateway"]
  gcp_labels             = local.common_gcp_labels
  bare_metal_hostnames   = try(var.bare_metal_hosts["monitoring"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["monitoring"].private_ips, [])
}

module "services" {
  source = "../compute"

  deployment_provider    = var.deployment_provider
  node_type              = "cpu_service"
  name_prefix            = "${var.name_prefix}-services"
  node_count             = var.service_node_count
  ssh_public_key         = var.ssh_public_key
  aws_ami_id             = var.aws_linux_ami_id
  aws_key_name           = var.aws_key_name
  aws_subnet_id          = module.network.core_subnet_id
  aws_security_group_ids = compact([module.network.core_internal_id, module.network.monitoring_id])
  aws_tags               = local.common_aws_tags
  gcp_project            = var.gcp_project
  gcp_zone               = var.gcp_zone
  gcp_subnetwork         = module.network.core_subnet_id
  gcp_tags               = ["core-node", "monitoring-target"]
  gcp_labels             = local.common_gcp_labels
  bare_metal_hostnames   = try(var.bare_metal_hosts["services"].hostnames, [])
  bare_metal_private_ips = try(var.bare_metal_hosts["services"].private_ips, [])
}

locals {
  central_vpn_host = (
    length(module.monitoring.public_ips) > 0
    ? module.monitoring.public_ips[0]
    : length(module.monitoring.private_ips) > 0
    ? module.monitoring.private_ips[0]
    : ""
  )
}
