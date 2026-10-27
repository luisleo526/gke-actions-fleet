locals {
  services = toset(["compute.googleapis.com", "container.googleapis.com", "iam.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com"])
}

# Bind each state to its deployment project. A wrong variable file must fail at
# plan time instead of replacing project-scoped IAM/API resources.
resource "terraform_data" "deployment_identity" {
  input            = var.project_id
  triggers_replace = [var.project_id]
  lifecycle { prevent_destroy = true }
}

resource "google_project_service" "required" {
  for_each           = local.services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
  depends_on         = [terraform_data.deployment_identity]
}

resource "google_compute_network" "runners" {
  name                    = var.cluster_name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "runners" {
  name                     = var.cluster_name
  region                   = var.region
  network                  = google_compute_network.runners.id
  ip_cidr_range            = "10.40.0.0/20"
  private_ip_google_access = true
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.44.0.0/16"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.45.0.0/20"
  }
}

resource "google_compute_router" "runners" {
  name    = var.cluster_name
  network = google_compute_network.runners.id
  region  = var.region
}

resource "google_compute_router_nat" "runners" {
  name                               = var.cluster_name
  router                             = google_compute_router.runners.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.runners.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

resource "google_service_account" "nodes" {
  account_id   = "${var.cluster_name}-nodes"
  display_name = "GKE runner node infrastructure only"
  depends_on   = [google_project_service.required]
}

resource "google_project_iam_member" "node_role" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

# One cluster for every size and CPU architecture. Private nodes use Cloud NAT.
resource "google_container_cluster" "runners" {
  name                     = var.cluster_name
  location                 = var.zone
  network                  = google_compute_network.runners.id
  subnetwork               = google_compute_subnetwork.runners.id
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.deletion_protection
  networking_mode          = "VPC_NATIVE"
  datapath_provider        = "ADVANCED_DATAPATH"
  enable_shielded_nodes    = true
  release_channel { channel = "REGULAR" }
  workload_identity_config { workload_pool = "${var.project_id}.svc.id.goog" }
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.20.0.0/28"
  }
  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = toset(var.admin_cidrs)
      content {
        cidr_block   = cidr_blocks.value
        display_name = "administrator"
      }
    }
  }
  cluster_autoscaling { autoscaling_profile = "OPTIMIZE_UTILIZATION" }
  logging_config { enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"] }
  monitoring_config { enable_components = ["SYSTEM_COMPONENTS"] }
  addons_config {
    gce_persistent_disk_csi_driver_config { enabled = true }
  }
  # Also constrain the temporary bootstrap pool: never use the default GCE SA.
  node_config {
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config { mode = "GKE_METADATA" }
    metadata = { disable-legacy-endpoints = "true" }
  }
  depends_on = [google_compute_router_nat.runners, google_project_iam_member.node_role]
  lifecycle {
    precondition {
      condition     = startswith(var.zone, "${var.region}-")
      error_message = "zone must belong to region."
    }
  }
}

# Controller and listeners must stay running to receive jobs when runner pools are zero.
resource "google_container_node_pool" "system" {
  name       = "system"
  cluster    = google_container_cluster.runners.id
  location   = var.zone
  node_count = 1
  management {
    auto_repair  = true
    auto_upgrade = true
  }
  node_config {
    machine_type    = "e2-standard-4"
    disk_size_gb    = 50
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = { "gke-gh-runner.io/pool" = "system" }
    metadata        = { disable-legacy-endpoints = "true" }
    workload_metadata_config { mode = "GKE_METADATA" }
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }
}

resource "google_container_node_pool" "runner" {
  for_each           = var.profiles
  name               = each.key
  cluster            = google_container_cluster.runners.id
  location           = var.zone
  initial_node_count = 0
  node_locations     = [coalesce(each.value.zone, var.zone)]
  autoscaling {
    total_min_node_count = 0
    total_max_node_count = each.value.max_runners
    location_policy      = "ANY"
  }
  management {
    auto_repair  = true
    auto_upgrade = true
  }
  node_config {
    machine_type    = each.value.machine_type
    spot            = each.value.spot
    disk_size_gb    = 30
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = {
      "gke-gh-runner.io/pool"    = "runner"
      "gke-gh-runner.io/profile" = coalesce(each.value.profile_name, each.key)
    }
    taint {
      key    = "gke-gh-runner.io/profile"
      value  = coalesce(each.value.profile_name, each.key)
      effect = "NO_SCHEDULE"
    }
    metadata = { disable-legacy-endpoints = "true" }
    workload_metadata_config { mode = "GKE_METADATA" }
    shielded_instance_config { enable_integrity_monitoring = true }
  }
}
