# Target-specific images and protected runner names

Each target has its own namespace and GitHub App Secret. Targets may register
with the same organization when their scale-set names do not overlap. The
existing GitHub runner groups and their repository/workflow allowlists are
managed separately; `runnerctl install` does not reset those settings.

## Per-target image

Set `targets.NAME.runner_image` to an immutable multi-platform image digest. It
overrides the optional global `runner_image`; the built-in official runner image
is the final fallback. The selected image is applied to the runner, workspace
initializer and DinD externals initializer. DinD itself uses its separate pinned
image. Preserve the supported runner UID, tools and sudo configuration in custom
images, and verify all requested architectures exist in the manifest.

## General and protected targets in one organization

An example `targets` object (replace the organization and image digest):

```json
{
  "general": {
    "github_url": "https://github.com/YOUR-ORG",
    "github_secret": "github-app",
    "runner_group": "gke-runners"
  },
  "protected": {
    "github_url": "https://github.com/YOUR-ORG",
    "github_secret": "github-app",
    "runner_group": "gke-protected",
    "runner_image": "ghcr.io/YOUR-ORG/toolchain@sha256:YOUR_64_HEX_DIGEST",
    "scale_set_prefix": "gkp-"
  }
}
```

These targets use `arc-general` and `arc-protected` respectively. Create the
App Secret independently in each namespace; identical Secret names in different
namespaces are separate objects. App credentials may be supplied through the
operator's existing organization installation; they are not passed to jobs.

For built-in `gke-*` profile keys, `scale_set_prefix` replaces the leading `gke-`.
Thus `gke-8cpu-16gb-amd64` becomes **`gkp-8cpu-16gb-amd64`**. For a custom profile
key without `gke-`, the prefix is prepended. Omitting the key preserves existing
plain profile names. Names must be valid and at most 45 characters; collisions
within the same GitHub organization/repository are rejected even across groups.

Credentialed workflows should explicitly use both their protected group and its
distinct name:

```yaml
runs-on:
  group: gke-protected
  labels: gkp-8cpu-16gb-amd64
```

General workflows retain `group: gke-runners` and `labels: gke-8cpu-16gb-amd64`.
The runner-group workflow allowlists are still required: a prefix is routing,
not authorization. Readback checks `spec.runnerScaleSetName` rather than assuming
the Helm/Kubernetes resource name is the GitHub routing name.

Targets share the existing machine/zone pools and capacity budgets by default.
This gives distinct registration, namespace and credential routing, not a
dedicated host boundary or reserved capacity. Dedicated target pools/taints are
not enabled by this configuration; introduce real infrastructure and capacity
allocation before claiming that stronger isolation.

## Pulling from a private Artifact Registry repository

Declare each existing repository explicitly at the top level:

```json
{
  "artifact_registry_repositories": [
    {
      "project_id": "your-gcp-project",
      "location": "us-central1",
      "repository": "runner-images"
    }
  ]
}
```

The Terraform configuration grants the node service account
`roles/artifactregistry.reader` **on that repository only**, using a repository
IAM member. It does not grant the role at project level, create the repository,
or put registry credentials in runner pods. The operator needs permission to
manage IAM on the specified repository. Review/apply the infrastructure plan
before installing targets that pull the private image. This optional list is
empty by default and does not grant private registry access implicitly from an
image URL.
