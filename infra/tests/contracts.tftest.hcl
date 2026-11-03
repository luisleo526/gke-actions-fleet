mock_provider "google" {}

run "dedicated_on_demand_pools_scale_from_zero_across_zones" {
  command = plan
  variables {
    profiles = {
      gke-od-8cpu-16gb-amd64      = { architecture = "amd64", machine_type = "n2d-custom-16-24576", max_runners = 2, spot = false }
      gke-od-8cpu-16gb-amd64-alt1 = { architecture = "amd64", machine_type = "n2d-custom-16-24576", max_runners = 2, spot = false, profile_name = "gke-od-8cpu-16gb-amd64", zone = "us-central1-b" }
    }
  }
  assert {
    condition     = alltrue([for p in google_container_node_pool.runner : !p.node_config[0].spot && p.initial_node_count == 0 && p.autoscaling[0].total_min_node_count == 0 && p.autoscaling[0].total_max_node_count == 2 && p.node_config[0].labels["gke-gh-runner.io/profile"] == "gke-od-8cpu-16gb-amd64"])
    error_message = "Release pools must have dedicated selectors, on-demand provisioning and zero resident nodes."
  }
  assert {
    condition     = google_container_node_pool.runner["gke-od-8cpu-16gb-amd64"].node_locations == toset(["us-central1-a"]) && google_container_node_pool.runner["gke-od-8cpu-16gb-amd64-alt1"].node_locations == toset(["us-central1-b"])
    error_message = "Release capacity must span two zones with one bounded budget."
  }
}

run "system_capacity_is_configurable_without_extra_runner_pools" {
  command = plan
  variables {
    system_node_count   = 4
    system_machine_type = "e2-standard-2"
  }
  assert {
    condition     = google_container_node_pool.system.node_count == 4 && google_container_node_pool.system.node_config[0].machine_type == "e2-standard-2" && length(google_container_node_pool.runner) == 4
    error_message = "Listener capacity must be configurable independently of runner pools."
  }
}

variables {
  project_id   = "test-runner-project"
  region       = "us-central1"
  zone         = "us-central1-a"
  cluster_name = "test-runners"
  admin_cidrs  = ["203.0.113.10/32"]
  profiles = {
    test-amd64      = { architecture = "amd64", machine_type = "n2-custom-4-12288", max_runners = 1, spot = true }
    test-amd64-alt1 = { architecture = "amd64", machine_type = "n2d-standard-4", max_runners = 1, spot = false, profile_name = "test-amd64", zone = "us-central1-b" }
    test-amd64-alt2 = { architecture = "amd64", machine_type = "n2-custom-4-12288", max_runners = 1, spot = true, profile_name = "test-amd64", zone = "us-central1-c" }
    test-arm64      = { architecture = "arm64", machine_type = "t2a-standard-4", max_runners = 2, spot = true }
  }
}

run "private_image_reader_is_repository_scoped" {
  command = plan
  variables {
    artifact_registry_repositories = [{ project_id = "test-runner-project", location = "us-central1", repository = "runner-images" }]
  }
  assert {
    condition     = google_artifact_registry_repository_iam_member.runner_reader["test-runner-project/us-central1/runner-images"].role == "roles/artifactregistry.reader" && google_artifact_registry_repository_iam_member.runner_reader["test-runner-project/us-central1/runner-images"].repository == "runner-images"
    error_message = "Image pulls must use an explicit repository-level reader binding."
  }
  assert {
    condition     = google_project_iam_member.node_role.role == "roles/container.defaultNodeServiceAccount"
    error_message = "Artifact Registry reader must not be granted at project level."
  }
}

run "one_cluster_with_zero_based_pools" {
  command = plan
  assert {
    condition     = google_container_node_pool.runner["test-amd64"].node_config[0].spot && google_container_node_pool.runner["test-arm64"].node_config[0].spot && !google_container_node_pool.runner["test-amd64-alt1"].node_config[0].spot && google_container_node_pool.runner["test-amd64-alt2"].node_config[0].spot
    error_message = "Both architectures must support Spot primaries and explicit Spot alternatives alongside on-demand fallbacks."
  }
  assert {
    condition     = length(google_container_node_pool.runner) == 4
    error_message = "Both architectures must share one cluster with distinct node pools."
  }
  assert {
    condition     = alltrue([for p in google_container_node_pool.runner : p.initial_node_count == 0 && p.autoscaling[0].total_min_node_count == 0 && p.autoscaling[0].total_max_node_count > 0])
    error_message = "All runner pools must scale from zero with a bounded global maximum."
  }
  assert {
    condition     = google_container_node_pool.runner["test-amd64-alt1"].node_config[0].labels["gke-gh-runner.io/profile"] == "test-amd64" && google_container_node_pool.runner["test-amd64-alt1"].node_config[0].taint[0].value == "test-amd64" && contains(google_container_node_pool.runner["test-amd64-alt1"].node_locations, "us-central1-b")
    error_message = "Alternative machine/zone pools must remain schedulable through the original profile selector and taint."
  }
  assert {
    condition     = google_container_node_pool.runner["test-amd64-alt2"].node_config[0].labels["gke-gh-runner.io/profile"] == "test-amd64" && google_container_node_pool.runner["test-amd64-alt2"].node_config[0].taint[0].value == "test-amd64" && contains(google_container_node_pool.runner["test-amd64-alt2"].node_locations, "us-central1-c")
    error_message = "Second-zone Spot alternatives must retain the original profile selector and taint."
  }
  assert {
    condition     = google_container_node_pool.system.node_count == 1 && google_container_node_pool.system.node_config[0].labels["gke-gh-runner.io/pool"] == "system"
    error_message = "One persistent system node is required for the controller/listeners."
  }
  assert {
    condition     = google_container_cluster.runners.private_cluster_config[0].enable_private_nodes && google_container_cluster.runners.datapath_provider == "ADVANCED_DATAPATH"
    error_message = "Private nodes and network-policy enforcement are required."
  }
  assert {
    condition     = alltrue([for p in google_container_node_pool.runner : p.node_config[0].taint[0].effect == "NO_SCHEDULE" && p.node_config[0].workload_metadata_config[0].mode == "GKE_METADATA"])
    error_message = "Runner pools require isolation taints and Workload Identity metadata isolation."
  }
}
