resource "aws_vpc" "this" {
  count = var.deployment_provider == "aws" ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(
    var.aws_tags,
    {
      Name      = "${var.name_prefix}-vpc"
      ManagedBy = "terraform"
    },
  )
}

resource "aws_subnet" "core" {
  count = var.deployment_provider == "aws" ? 1 : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.core_subnet_cidr
  map_public_ip_on_launch = false

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-core" })
}

resource "aws_subnet" "edge" {
  count = var.deployment_provider == "aws" ? 1 : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.edge_subnet_cidr
  map_public_ip_on_launch = false

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-edge" })
}

resource "aws_subnet" "camera" {
  count = var.deployment_provider == "aws" ? 1 : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.camera_subnet_cidr
  map_public_ip_on_launch = false

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-camera" })
}

resource "aws_security_group" "core_internal" {
  count = var.deployment_provider == "aws" ? 1 : 0

  name        = "${var.name_prefix}-core-internal"
  description = "Allow full east-west access across core nodes."
  vpc_id      = aws_vpc.this[0].id

  ingress {
    description = "Core node east-west traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.core_subnet_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-core-internal" })
}

resource "aws_security_group" "edge_to_core" {
  count = var.deployment_provider == "aws" ? 1 : 0

  name        = "${var.name_prefix}-edge-to-core"
  description = "Allow edge-originated NATS and Kafka traffic into core."
  vpc_id      = aws_vpc.this[0].id

  ingress {
    description = "NATS from edge"
    from_port   = 4222
    to_port     = 4222
    protocol    = "tcp"
    cidr_blocks = [var.edge_subnet_cidr]
  }

  ingress {
    description = "Kafka from edge"
    from_port   = 9092
    to_port     = 9094
    protocol    = "tcp"
    cidr_blocks = [var.edge_subnet_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-edge-to-core" })
}

resource "aws_security_group" "camera_to_edge" {
  count = var.deployment_provider == "aws" ? 1 : 0

  name        = "${var.name_prefix}-camera-to-edge"
  description = "Allow RTSP only from the camera VLAN into edge gateways."
  vpc_id      = aws_vpc.this[0].id

  ingress {
    description = "RTSP from camera VLAN"
    from_port   = 554
    to_port     = 554
    protocol    = "tcp"
    cidr_blocks = [var.camera_subnet_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-camera-to-edge" })
}

resource "aws_security_group" "monitoring" {
  count = var.deployment_provider == "aws" ? 1 : 0

  name        = "${var.name_prefix}-monitoring"
  description = "Allow Prometheus and service metrics from the monitoring subnet."
  vpc_id      = aws_vpc.this[0].id

  ingress {
    description = "Prometheus"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = [var.core_subnet_cidr]
  }

  ingress {
    description = "Node Exporter"
    from_port   = 9100
    to_port     = 9100
    protocol    = "tcp"
    cidr_blocks = [var.core_subnet_cidr]
  }

  ingress {
    description = "Service metrics"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [var.core_subnet_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-monitoring" })
}

resource "aws_security_group" "public_api" {
  count = var.deployment_provider == "aws" ? 1 : 0

  name        = "${var.name_prefix}-public-api"
  description = "Expose HTTPS only on the designated API gateway."
  vpc_id      = aws_vpc.this[0].id

  ingress {
    description = "Public HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.public_https_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.aws_tags, { Name = "${var.name_prefix}-public-api" })
}

resource "google_compute_network" "this" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project                 = var.gcp_project
  name                    = "${var.name_prefix}-network"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "core" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project       = var.gcp_project
  region        = var.gcp_region
  name          = "${var.name_prefix}-core"
  ip_cidr_range = var.core_subnet_cidr
  network       = google_compute_network.this[0].id
}

resource "google_compute_subnetwork" "edge" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project       = var.gcp_project
  region        = var.gcp_region
  name          = "${var.name_prefix}-edge"
  ip_cidr_range = var.edge_subnet_cidr
  network       = google_compute_network.this[0].id
}

resource "google_compute_subnetwork" "camera" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project       = var.gcp_project
  region        = var.gcp_region
  name          = "${var.name_prefix}-camera"
  ip_cidr_range = var.camera_subnet_cidr
  network       = google_compute_network.this[0].id
}

resource "google_compute_firewall" "core_internal" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project = var.gcp_project
  name    = "${var.name_prefix}-core-internal"
  network = google_compute_network.this[0].name

  source_ranges = [var.core_subnet_cidr]
  target_tags   = ["core-node"]

  allow {
    protocol = "all"
  }
}

resource "google_compute_firewall" "edge_to_core" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project = var.gcp_project
  name    = "${var.name_prefix}-edge-to-core"
  network = google_compute_network.this[0].name

  source_ranges = [var.edge_subnet_cidr]
  target_tags   = ["core-node"]

  allow {
    protocol = "tcp"
    ports    = ["4222", "9092-9094"]
  }
}

resource "google_compute_firewall" "camera_to_edge" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project = var.gcp_project
  name    = "${var.name_prefix}-camera-to-edge"
  network = google_compute_network.this[0].name

  source_ranges = [var.camera_subnet_cidr]
  target_tags   = ["edge-gateway"]

  allow {
    protocol = "tcp"
    ports    = ["554"]
  }
}

resource "google_compute_firewall" "monitoring" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project = var.gcp_project
  name    = "${var.name_prefix}-monitoring"
  network = google_compute_network.this[0].name

  source_ranges = [var.core_subnet_cidr]
  target_tags   = ["monitoring-target"]

  allow {
    protocol = "tcp"
    ports    = ["8080", "9090", "9100"]
  }
}

resource "google_compute_firewall" "public_api" {
  count = var.deployment_provider == "gcp" ? 1 : 0

  project = var.gcp_project
  name    = "${var.name_prefix}-public-api"
  network = google_compute_network.this[0].name

  source_ranges = var.public_https_cidrs
  target_tags   = ["api-gateway"]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "null_resource" "bare_metal_network" {
  count = var.deployment_provider == "bare_metal" ? 1 : 0

  triggers = {
    core_subnet   = var.core_subnet_cidr
    edge_subnet   = var.edge_subnet_cidr
    camera_subnet = var.camera_subnet_cidr
  }
}
