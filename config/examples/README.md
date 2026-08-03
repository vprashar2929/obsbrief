# Config Examples

These files are example starting points for configuring `opsbrief`.

- `auto-discovery.yaml`: provide project scope and let `opsbrief` discover clusters,
  compute instances, and load balancers.
- `explicit-clusters.yaml`: provide cluster names directly and disable discovery for
  clusters, compute instances, and load balancers.

For day-to-day CLI usage, `config/runtime.yaml` is the smallest baseline and can be
combined with runtime flags such as `--project`, `--region`, and `--cluster`.
