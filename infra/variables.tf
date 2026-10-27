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
