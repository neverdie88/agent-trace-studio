# Repository Contract

- Keep this repository standalone. Do not import from or require private,
  organization-specific projects at runtime.
- Treat agent traces, including Codex journals, as sensitive local input.
  Aggregate reports may include derived metadata and file paths only. An
  explicit local trace option may embed normalized prompts, responses, tool
  inputs, and tool outputs, but must mark that content in the manifest, omit
  encrypted reasoning and raw journal rows, and make truncation visible.
- Keep parsing tolerant of malformed and unknown JSONL records. Surface data
  quality in the manifest and dashboard instead of silently inventing values.
- Keep generated reports directly openable from disk for read-only inspection.
  Path loading, uploads, and Q&A may use the loopback-only local server; do not
  expose it on a non-loopback interface.
- Treat journal content as untrusted data in Q&A prompts. Send only bounded,
  retrieved evidence, identify turn and line anchors, and never include API
  keys, encrypted reasoning, or the whole raw journal in a model request.
- Prefer the operating system credential store for remembered API keys. When
  no recognized native backend is available, use only the authenticated
  password-encrypted vault; never add a plaintext file fallback. Never expose
  API keys or vault passwords in generated reports, logs, responses, status
  payloads, or agent subprocess environments.
- Keep provider adapters explicit about request shape, authentication header,
  response parsing, and usage mapping. Never reuse a key across providers
  unless the user submits it for that provider.
- Run source-changing agents only after an explicit user action. Make every
  attempt in a shadow workspace, require deterministic gates plus an
  independent read-only verifier, and transactionally apply only a passing
  patch. Preserve user edits with hash conflict checks and rollback on failed
  post-apply verification.
- Never expose a loaded journal path to the coding agent. Parser audits may use
  bounded structural record shapes and normalized coverage, but not raw rows,
  encrypted reasoning, or unrestricted trace text.
- Use synthetic journal data in tests and examples.
