# Sample Report

[sample-report.md](sample-report.md) is a generated reference report containing twelve collector
sections and a mixture of finding statuses. It is derived from a real report's *shape* only: all
organization names, infrastructure identifiers, text evidence, timestamps, and numerical values
have been replaced with deterministic sample data.

The replay source is [examples/sample-report.json](../../examples/sample-report.json). Regenerate
the Markdown, HTML, PDF, JSON, and evidence artifacts without cloud access:

```bash
hatch run opsbrief weekly \
  --from-report-json examples/sample-report.json \
  --brand-profile config/brand.example.yaml \
  --include-evidence-index \
  --output-dir ./reports
```

The generated PDF and HTML are intentionally not committed: they are binary/large presentation
artifacts, while the checked-in Markdown and JSON remain reviewable and reproducible.
