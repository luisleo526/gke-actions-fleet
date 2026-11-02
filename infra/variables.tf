variable "project_id" { type = string }
variable "region" { type = string }
variable "zone" { type = string }
variable "cluster_name" { type = string }
variable "admin_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for c in var.admin_cidrs : can(cidrhost(c, 0)) && !endswith(c, "/0")])
    error_message = "Supply explicit administrator CIDRs; public /0 access is prohibited."
  }
}
variable "profiles" {
  type = map(object({
    architecture = string
    machine_type = string
    max_runners  = number
    spot         = bool
    profile_name = optional(string)
    zone         = optional(string)
  }))
  validation {
    condition     = length(var.profiles) > 0 && alltrue([for p in var.profiles : contains(["amd64", "arm64"], p.architecture) && p.max_runners >= 1 && floor(p.max_runners) == p.max_runners])
    error_message = "Profiles need an amd64/arm64 architecture and a positive integral capacity."
  }
}
variable "deletion_protection" {
  type    = bool
  default = true
}

variable "system_node_count" {
  description = "Fixed system capacity for ARC listeners and cluster services. Grow before a machine-type reduction."
  type        = number
  default     = 1
  validation {
    condition     = var.system_node_count >= 1 && var.system_node_count <= 20 && floor(var.system_node_count) == var.system_node_count
    error_message = "system_node_count must be an integer in 1..20."
  }
}

variable "system_machine_type" {
  type    = string
  default = "e2-standard-4"
  validation {
    condition     = contains(["e2-standard-2", "e2-standard-4", "e2-standard-8", "e2-standard-16", "e2-standard-32"], var.system_machine_type)
    error_message = "Use a supported E2 standard system machine."
  }
}

variable "artifact_registry_repositories" {
  description = "Explicit existing repositories the node service account may pull from. No project-level Artifact Registry role is granted."
  type = list(object({
    project_id = string
    location   = string
    repository = string
  }))
  default = []
}
