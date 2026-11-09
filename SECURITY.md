# Security

Do not report credentials or exploitable security issues in public issues. Once private vulnerability reporting is enabled, use the repository's **Security → Report a vulnerability** form. Before publication, report privately to a repository administrator through your existing trusted channel. No dedicated mailbox is provisioned by this change.

Include the affected version, reproduction steps using synthetic data, and expected impact. Never attach App private keys, llmux keys, model credentials, production configuration, or full private PR logs.

Keep the bot's GitHub App scoped to selected repositories. Contents write is optional and grants more authority than basic review posting. Automatic approvals remain disabled unless explicitly configured. Model output is untrusted; review findings are advisory and do not replace your tests or human judgment.

The bot reads repository code and sends selected context to the configured llmux/model provider. Local review artifacts may include source and model responses. Protect the service account, its configuration directory, and backups.
