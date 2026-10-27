output "cluster_name" { value = google_container_cluster.runners.name }
output "location" { value = google_container_cluster.runners.location }
output "runner_pools" {
  value = { for name, pool in google_container_node_pool.runner : name => {
    architecture    = var.profiles[name].architecture
    profile_name    = coalesce(var.profiles[name].profile_name, name)
    zone            = coalesce(var.profiles[name].zone, var.zone)
    machine_type    = var.profiles[name].machine_type
    maximum         = var.profiles[name].max_runners
    instance_groups = pool.managed_instance_group_urls
  } }
}
