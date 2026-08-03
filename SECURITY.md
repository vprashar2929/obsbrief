# Security Policy

## Supported Versions

Security fixes are applied to the current main branch and the latest published release, when a
release exists.

## Reporting a Vulnerability

Do not open a public issue with exploit details, credentials, customer identifiers, private
hostnames, or report artifacts.

Use the repository's private vulnerability reporting channel if it is available. If private
reporting is not available, open a public issue requesting a security contact and include only a
brief, non-sensitive summary.

Please include:

- Affected version or commit.
- A short description of the impact.
- Reproduction steps using sanitized data.
- Any relevant logs with secrets removed.

## Security Expectations

`opsbrief` is designed for read-only evidence collection. Security-sensitive changes should preserve
these constraints:

- No mutating GCP, Kubernetes, or external service operations.
- No credential persistence.
- No committed secrets, customer identifiers, or private hostnames.
- No automatic upload, email, ticket, chat, or storage side effects without an explicit design review.
