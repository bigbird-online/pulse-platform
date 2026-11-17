# Security Policy

## Supported versions

Only the latest commit on `main` receives security fixes.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Report them privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**.

Include what is affected, how to reproduce it, and the impact you expect. We aim to acknowledge reports within 3 business days and to agree on a fix and disclosure date with you.

## Scope

In scope: Relay's access control (allowlists, pairing), workspace write boundaries, the loopback API, secret handling, and the other modules in this repository.

Out of scope: vulnerabilities in third-party services and CLIs (Feishu, Telegram, OpenAI Codex, Claude Code) unless PULSE uses them in an unsafe way, and issues that require an already compromised server account.

## If you leaked a secret

If a token or app secret was published in an issue, log or commit, revoke it with its provider first (Feishu Open Platform, BotFather, OpenAI, Anthropic), then tell us so we can remove the copy.
