# Library Strategy

OpsBrief should use mature OSS libraries for infrastructure plumbing and keep
deployment-specific operational interpretation in this repository.

## Keep Custom

- Collector scoring and status mapping.
- Evidence-to-finding interpretation.
- Client/environment policy handling.
- Report wording and stakeholder-facing operational summaries.

These areas encode product and delivery context. Generic libraries should not
own those decisions.

## Prefer Libraries

- Cloud and Kubernetes transport/auth clients.
- API pagination and retry plumbing.
- HTTP transport, timeout, and JSON error handling.
- Config validation.
- Markdown, HTML, PDF, and chart rendering engines.

When a collector needs a new integration, prefer an official client library
first. Use lower-level HTTP or discovery clients only when the official client is
unavailable, incomplete for the required API, or materially harder to test.

Cloud SQL Admin is a deliberate exception for Python: the official Cloud SQL
Admin client-library guidance still uses the Google APIs Discovery Service with
the `sqladmin` API name. Keep Cloud SQL calls behind `opsbrief.gcp_api` helpers so
the collector code does not duplicate discovery request construction.

Cloud DNS response policies are also discovery-backed in Python because the
installed `google-cloud-dns` client exposes managed zones and record sets, but
not response-policy resources. Keep Compute and DNS discovery services lazy and
fallback-only when official clients can provide the same evidence.

## Shared Adapters

Collectors should use shared adapter helpers instead of duplicating setup code:

- `opsbrief.gcp_api` for Google API client construction and pagination helpers.
- `opsbrief.k8s_api` for Kubernetes endpoint, token, CA, and API client setup.
- `opsbrief.status` for shared cloud error classification.

Add to these adapters when multiple collectors need the same plumbing. Keep the
adapters small and behavior-preserving; do not hide collector-specific evidence
logic behind generic abstractions.

## Migration Order

1. Centralize duplicated plumbing behind internal adapters.
2. Replace raw HTTP transport with a maintained HTTP client where needed.
3. Move config validation to a schema library when the current hand-written
   validation becomes a blocker.
4. Move stable report layout shells to Jinja templates while keeping report
   context construction, collector interpretation, and table row generation in
   Python.
5. Migrate discovery-style Google API calls to Cloud Client Libraries one
   collector at a time where they reduce code and preserve evidence shape.
