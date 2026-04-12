locals {
  module_prefix = "${var.name_prefix}-${var.site_id}"

  common_aws_tags = merge(
    var.aws_tags,
    {
      ManagedBy   = "terraform"
      SiteId      = var.site_id
      SiteName    = var.site_name
      CameraCount = tostring(var.camera_count)
      Role        = "site-edge"
    },
  )

  common_gcp_labels = merge(
    var.gcp_labels,
    {
      managed_by   = "terraform"
      site_id      = replace(var.site_id, "-", "_")
      camera_count = tostring(var.camera_count)
      role         = "site-edge"
    },
  )
}

module "network" {
  source = "../network"

  deployment_provider = var.deployment_provider
  name_prefix         = local.module_prefix
  vpc_cidr            = var.vpc_cidr
  core_subnet_cidr    = var.site_core_subnet_cidr
  edge_subnet_cidr    = var.edge_subnet_cidr
  camera_subnet_cidr  = var.camera_subnet_cidr
  public_https_cidrs  = var.public_https_cidrs
  aws_tags            = local.common_aws_tags
  gcp_project         = var.gcp_project
  gcp_region          = var.gcp_region
  gcp_labels          = local.common_gcp_labels
}

resource "aws_security_group" "site_vpn" {
  count = var.deployment_provider == "aws" ? 1 : 0

  name        = "${local.module_prefix}-wireguard"
  description = "Allow central WireGuard peers into the site edge gateways."
  vpc_id      = module.network.network_id

  ingress {
    description = "WireGuard from the central network"
    from_port   = var.wireguard_port
    to_port     = var.wireguard_port
    protocol    = "udp"
    cidr_blocks = var.central_vpn_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_aws_tags, { Name = "${local.module_prefix}-wireguard" })
}

resource "google_compute_firewall" "site_vpn" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project = var.gcp_project
  name    = "${local.module_prefix}-wireguard"
  network = module.network.network_id

  source_ranges = var.central_vpn_cidrs
  target_tags   = ["edge-gateway", "site-${var.site_id}"]

  allow {
    protocol = "udp"
    ports    = [tostring(var.wireguard_port)]
  }
}

resource "null_resource" "site_vpn" {
  count = var.deployment_provider == "bare_metal" ? 1 : 0

  triggers = {
    site_id         = var.site_id
    central_cidrs   = join(",", var.central_vpn_cidrs)
    wireguard_port  = tostring(var.wireguard_port)
    site_network_id = module.network.network_id
  }
}

module "edge_gateways" {
  source = "../compute"

  deployment_provider       = var.deployment_provider
  node_type                 = "edge_gateway"
  name_prefix               = "${local.module_prefix}-edge"
  node_count                = var.edge_count
  assign_public_ip          = var.assign_public_ip
  ssh_public_key            = var.ssh_public_key
  aws_ami_id                = var.aws_ami_id
  aws_key_name              = var.aws_key_name
  aws_subnet_id             = module.network.edge_subnet_id
  aws_security_group_ids    = compact([module.network.edge_to_core_id, module.network.camera_to_edge_id, module.network.monitoring_id, try(aws_security_group.site_vpn[0].id, null)])
  aws_tags                  = local.common_aws_tags
  gcp_project               = var.gcp_project
  gcp_zone                  = var.gcp_zone
  gcp_subnetwork            = module.network.edge_subnet_id
  gcp_image                 = var.gcp_image
  gcp_service_account_email = var.gcp_service_account_email
  gcp_tags                  = distinct(concat(var.gcp_tags, ["edge-gateway", "monitoring-target", "site-${var.site_id}"]))
  gcp_labels                = local.common_gcp_labels
  bare_metal_hostnames      = var.bare_metal_hostnames
  bare_metal_private_ips    = var.bare_metal_private_ips
}

resource "aws_ebs_volume" "site_storage" {
  count = var.deployment_provider == "aws" ? var.edge_count : 0

  availability_zone = var.aws_availability_zone
  size              = var.site_storage_size_gb
  iops              = var.site_storage_iops
  type              = var.site_storage_volume_type

  tags = merge(
    local.common_aws_tags,
    { Name = format("%s-site-storage-%02d", local.module_prefix, count.index + 1) },
  )
}

resource "google_compute_disk" "site_storage" {
  count = var.deployment_provider == "gcp" ? var.edge_count : 0

  name   = format("%s-site-storage-%02d", local.module_prefix, count.index + 1)
  zone   = var.gcp_zone
  size   = var.site_storage_size_gb
  type   = var.site_storage_volume_type == "io2" ? "pd-ssd" : "pd-balanced"
  labels = local.common_gcp_labels
}

resource "null_resource" "site_storage" {
  count = var.deployment_provider == "bare_metal" ? var.edge_count : 0

  triggers = {
    name = format("%s-site-storage-%02d", local.module_prefix, count.index + 1)
    size = tostring(var.site_storage_size_gb)
    iops = tostring(var.site_storage_iops)
  }
}

locals {
  vpn_host = (
    length(module.edge_gateways.public_ips) > 0
    ? module.edge_gateways.public_ips[0]
    : length(module.edge_gateways.private_ips) > 0
    ? module.edge_gateways.private_ips[0]
    : ""
  )

  site_vpn_endpoint = local.vpn_host != "" ? "udp://${local.vpn_host}:${var.wireguard_port}" : ""
  site_storage_endpoint = (
    length(module.edge_gateways.private_ips) > 0
    ? "http://${module.edge_gateways.private_ips[0]}:${var.site_storage_port}"
    : ""
  )
}
