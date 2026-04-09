locals {
  aws_instance_types = {
    gpu_inference = "p3.2xlarge"
    cpu_service   = "m5.xlarge"
    edge_gateway  = "t3.large"
    monitoring    = "m5.xlarge"
  }

  gcp_machine_types = {
    gpu_inference = "n1-standard-8"
    cpu_service   = "n1-standard-4"
    edge_gateway  = "n1-standard-2"
    monitoring    = "n1-standard-4"
  }

  effective_aws_ami = (
    var.node_type == "gpu_inference" && var.aws_gpu_ami_id != ""
    ? var.aws_gpu_ami_id
    : var.aws_ami_id
  )

  effective_gcp_image = (
    var.node_type == "gpu_inference" && var.gcp_gpu_image != ""
    ? var.gcp_gpu_image
    : var.gcp_image
  )
}

resource "aws_instance" "nodes" {
  count = var.provider == "aws" ? var.node_count : 0

  ami                         = local.effective_aws_ami
  instance_type               = local.aws_instance_types[var.node_type]
  subnet_id                   = var.aws_subnet_id
  vpc_security_group_ids      = var.aws_security_group_ids
  key_name                    = var.aws_key_name
  associate_public_ip_address = var.assign_public_ip

  root_block_device {
    volume_size = var.aws_root_volume_size_gb
    volume_type = "gp3"
  }

  tags = merge(
    var.aws_tags,
    {
      Name      = format("%s-%02d", var.name_prefix, count.index + 1)
      NodeType  = var.node_type
      ManagedBy = "terraform"
    },
  )
}

resource "google_compute_instance" "nodes" {
  count = var.provider == "gcp" ? var.node_count : 0

  project      = var.gcp_project
  zone         = var.gcp_zone
  name         = format("%s-%02d", var.name_prefix, count.index + 1)
  machine_type = local.gcp_machine_types[var.node_type]

  boot_disk {
    initialize_params {
      image = local.effective_gcp_image
      size  = var.gcp_boot_disk_size_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = var.gcp_subnetwork

    dynamic "access_config" {
      for_each = var.assign_public_ip ? [1] : []

      content {}
    }
  }

  metadata = var.ssh_public_key != "" ? {
    ssh-keys = "cilex:${var.ssh_public_key}"
  } : {}

  dynamic "guest_accelerator" {
    for_each = var.node_type == "gpu_inference" ? [1] : []

    content {
      type  = var.gcp_gpu_accelerator_type
      count = 1
    }
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = var.node_type == "gpu_inference" ? "TERMINATE" : "MIGRATE"
  }

  service_account {
    email  = var.gcp_service_account_email != null ? var.gcp_service_account_email : "default"
    scopes = ["cloud-platform"]
  }

  tags = distinct(concat(var.gcp_tags, ["cilex", replace(var.node_type, "_", "-")]))

  labels = merge(
    var.gcp_labels,
    {
      role       = replace(var.node_type, "_", "-")
      managed_by = "terraform"
    },
  )
}

resource "null_resource" "nodes" {
  count = var.provider == "bare_metal" ? length(var.bare_metal_hostnames) : 0

  triggers = {
    hostname = var.bare_metal_hostnames[count.index]
    private_ip = (
      count.index < length(var.bare_metal_private_ips)
      ? var.bare_metal_private_ips[count.index]
      : ""
    )
    node_type = var.node_type
  }
}
