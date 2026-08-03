# Config Examples

These files are sanitized starting points for public or client-neutral usage.

- `auto-discovery.yaml`: provide project scope and let `opsbrief` discover clusters,
  compute instances, and load balancers.
- `explicit-clusters.yaml`: provide cluster names directly and disable discovery for
  clusters, compute instances, and load balancers.

For day-to-day CLI usage, `config/runtime.yaml` is the smallest baseline and can be
combined with runtime flags such as `--project`, `--region`, and `--cluster`.

Keep organization-specific configs, brand profiles, logos, and brand guidelines outside
this public repository.
