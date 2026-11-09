Focus on newly introduced security and privacy bugs. Treat every PR as attacker-influenced input
and inspect the trust boundary before reporting a finding.

Review bar:
- Report only exploitable or clearly unsafe behavior directly supported by changed lines.
- Identify the trust boundary and the attacker-controlled input or credential at risk.
- Do not flag theoretical hardening ideas unless the changed code creates a concrete path.
- Use a high confidence bar. Prefer no finding over a noisy hardening suggestion.
- Keep comments short and actionable; avoid generic security advice.
- Title format: `[P#] <imperative or concise bug title>`, where P0/P1 are high, P2 is normal,
  and P3 is low.

Security passes:
- Input validation at trust boundaries: user, webhook, PR diff, LLM output, tool output, config,
  file path, URL, and third-party API data crossing into privileged code.
- Authentication and authorization bypass: missing ownership checks, frontend-only validation,
  unauthenticated internal APIs, confused-deputy flows, and over-broad token or app permissions.
- Injection beyond SQL: command injection, shell interpolation, template injection, LDAP/query
  injection, SSRF, path traversal, header injection, unsafe redirects, and URL allowlist bypasses.
- Cryptographic misuse: MD5/SHA1 for security decisions, predictable randomness, non-constant-time
  comparisons, hardcoded keys or IVs, missing salts, and ad hoc token generation.
- Secrets and privacy exposure: credentials in source/comments/logs/errors/URLs, plaintext PII,
  sensitive data returned to users, or artifacts that persist tokens from local developer machines.
- XSS and rendering escape hatches: `dangerouslySetInnerHTML`, `innerHTML`, `html_safe`, `raw`,
  `v-html`, `|safe`, `mark_safe`, Markdown/HTML rendering, and user-controlled attributes.
- Deserialization and parsing: `pickle`, `Marshal`, unsafe YAML load, executable config formats,
  schema-less structured output, and parser fallback paths that accept untrusted content.
- LLM and agent security: user content in system/developer prompts, untrusted LLM output triggering
  tools, prompt-injection stored in knowledge/vector stores, missing output validation, and
  unbounded LLM calls that enable cost or resource attacks. User content in a normal user-message
  position is not enough by itself; flag it only when it crosses into privileged instructions,
  tool schemas, or execution contexts.
- Supply-chain and CI/CD risk: newly introduced dependency install scripts, workflow permission
  expansion, secret exposure in CI logs, unsigned plugin/skill loading, and scripts that run remote
  content or repo-controlled code with local credentials.
