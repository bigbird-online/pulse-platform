# PULSE

PULSE is a self-hosted foundation for working with AI agents: you chat with an agent from Feishu (Lark) or Telegram, the agent runs on your own server with your own subscription, and optional modules track work items, cost and usage.

[中文说明](README.zh-CN.md)

**Status:** early public release. Relay (the chat gateway and agent runtimes) is the supported entry point and the only module with an end-to-end install guide. The other modules ship with their source and tests; their install guides will follow.

## Quick start: Relay only (Feishu + Codex + `gpt-6.1-sol`)

You need a Linux server (Ubuntu 24.04 or newer) with `bubblewrap` installed for the Codex sandbox, Node.js 22, a ChatGPT Plus (or higher) account for Codex, and a Feishu self-built app. The [full guide](docs/quickstart-feishu-codex.md) walks through every step and the five acceptance checks.

```bash
sudo apt-get install -y bubblewrap   # Codex sandbox; Ubuntu 24.04 also needs the bwrap AppArmor profile (see the guide, step 1)
git clone https://github.com/bigbird-online/pulse-platform.git ~/pulse
cd ~/pulse/Relay
npm ci && npm run build

# Sign in to Codex with your ChatGPT account (device code, no browser on the server needed)
./node_modules/.bin/codex login --device-auth
./node_modules/.bin/codex exec --skip-git-repo-check -m gpt-6.1-sol "Say hello in one sentence"
```

Then write a minimal `~/.config/relay/relay.json`:

```json
{
  "users": [{
    "userId": "owner",
    "defaultAgent": "assistant",
    "agents": [{
      "id": "assistant",
      "name": "Assistant",
      "emoji": "🤖",
      "channel": "feishu",
      "runtime": "codex",
      "workspace": "/home/relay/workspace",
      "feishu": { "appId": "<APP_ID>", "appSecret": "<APP_SECRET>", "sessionId": "<any UUID>" }
    }]
  }]
}
```

Start it (the guide shows the same as a systemd user service):

```bash
RELAY_CONFIG_PATH=~/.config/relay/relay.json \
RELAY_DATA_DIR=~/.local/share/relay \
RELAY_CODEX_MODEL=gpt-6.1-sol \
RELAY_HTTP_PORT=8788 RELAY_INTERNAL_TOKEN="$(openssl rand -hex 24)" \
node ~/pulse/Relay/dist/cli.js
```

Send your bot a message in Feishu. The first message returns a pairing code; approve it on the server and the assistant starts answering.

## Features

- **Chat channels**: Feishu/Lark (long-lived WebSocket connection, no public IP needed) and Telegram (long polling).
- **Agent runtimes**: OpenAI Codex CLI (default), Claude Code (`cc`, `cc-sdk`) and Pi. Each agent picks one runtime; a node can define fallback order.
- **Per-user allowlist**: only paired or configured users reach an agent; everyone else gets a pairing code or a refusal.
- **Bounded workspace**: each agent works in one directory; Codex runs in its `workspace-write` sandbox.
- **Streaming replies**: progress cards, folded work process, final answer delivered separately.
- **Scheduler**: cron-style jobs that run an agent turn and deliver the result to chat (batch runs use the Python owned-process helper described under [Runtimes](#runtimes)).
- **Loopback API**: `POST /internal/push` and friends let local programs send messages through your bots.
- **Optional platform modules**: work-item kernel, compute cost accounting, usage dashboard, tenant onboarding.

## Architecture

```text
 Feishu / Telegram
        │
        ▼
┌──────────────────────── Relay (Node.js) ────────────────────────┐
│ channels → router (users, agents, pairing) → conversation pool  │
│ scheduler · loopback HTTP API (127.0.0.1)                       │
└───────────────┬──────────────────────────────┬──────────────────┘
                │ spawns                       │ optional (PULSE_RELAY_PATHS)
                ▼                              ▼
   Codex CLI / Claude Code / Pi        agent-harness (work-item kernel)
   in the agent workspace              ComputeCost → Stats
```

| Directory | What it is | Status |
| --- | --- | --- |
| `Relay/` | Chat gateway, agent runtimes, scheduler, loopback API (TypeScript) | Supported; install guide available |
| `agent-harness/` | Work-item (WI) kernel: execution grants, collaboration contracts, review gate (Python) | Source and tests; guide to follow |
| `ComputeCost/` | Attributes model usage and cost to agents and work items (Python) | Source and tests; guide to follow |
| `Stats/` | Cost and usage dashboard: exporter, publisher, macOS app | Source and tests; guide to follow |
| `Platform/` | Tenant onboarding web flow (TypeScript) | Source and tests; guide to follow |
| `Scripts/` | Helpers Relay runs (the owned-process launcher). Deployment and operations tooling is not published | Runtime helper |
| `specs/`, `*/specs/` | Product specifications; the code and tests are the source of truth | Reference |
| `skills/` | Agent skills that use the platform (for example Relay push) | Reference |

## Configuration

Relay reads one JSON file (`RELAY_CONFIG_PATH`). See [`Relay/config/relay.example.json`](Relay/config/relay.example.json) for every field. Common environment variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `RELAY_CONFIG_PATH` | Path to `relay.json` | `<repo>/data/relay.json` |
| `RELAY_DATA_DIR` | Sessions and other state written by Relay | `~/.relay` |
| `CRON_JOBS_PATH` | Scheduler job file | `<repo>/data/cron-jobs.json` |
| `METERING_DIR` | Usage records | `<repo>/data/metering/` |
| `RELAY_CODEX_MODEL` | Codex model | `gpt-6-astra` |
| `RELAY_CODEX_REASONING_EFFORT` | Codex reasoning effort | `xhigh` |
| `RELAY_HTTP_PORT` | Enables the loopback API on this port | disabled |
| `RELAY_HTTP_HOST` | Bind address of the loopback API | `127.0.0.1` |
| `RELAY_INTERNAL_TOKEN` | Required `x-relay-token` header for the loopback API | unset: no token check |
| `PULSE_COEVOLUTION_ROOT` | Extra directory Codex may write to (`--add-dir`) | unset: none |
| `PULSE_RELAY_PATHS` | Managed mode: path map that turns on the work-item kernel | unset: standalone |

Keep `relay.json` in a `0700` directory with mode `0600`: it holds channel secrets.

## Runtimes

| Runtime | Needs | Notes |
| --- | --- | --- |
| `codex` (default) | Bundled `@openai/codex`; ChatGPT login or API key | `codex exec` with `-a never -s workspace-write -C <workspace>` |
| `cc` | Claude Code CLI; Python 3.11+ at `~/.local/share/coevolution/python-env/bin/python` for the owned-process helper (a known hard-coded path) | Interactive process pool. Scheduled batch runs of every runtime use the same helper |
| `cc-sdk` | Optional dependency `@anthropic-ai/claude-agent-sdk` | In-process SDK sessions; without the package this runtime reports that it is missing |
| `pi` | A Pi runtime manifest (`RELAY_PI_RUNTIME_MANIFEST_PATH`) | Optional; see `Relay/specs/pi-conversation-runtime.md` |

## Security model

- **Who can talk to an agent**: Telegram agents accept only configured chat IDs. Feishu agents accept only bound users; an unknown user gets a five-minute pairing code and no agent reply until the owner approves it on the server.
- **Where an agent can write**: Codex runs with `-s workspace-write` in the agent's `workspace`. In standalone mode Relay adds no other writable root unless you set `PULSE_COEVOLUTION_ROOT`. Do not enable `codexYoloDefault` for agents that strangers can reach.
- **Loopback API**: binds to `127.0.0.1` by default. Set `RELAY_INTERNAL_TOKEN` so other local users cannot call it.
- **Secrets**: channel credentials live only in your `relay.json` (or the file named by `RELAY_SECRETS_ENV_FILE`); Codex credentials live in `~/.codex/auth.json`. Treat both like passwords.
- Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## From Relay to the full PULSE platform

Relay runs standalone when `PULSE_RELAY_PATHS` is unset: the work-item commands, standing grants and session proofs stay off, and nothing outside `Relay/` is needed for the Codex path. Setting `PULSE_RELAY_PATHS` switches Relay to managed mode and connects it to the work-item kernel in `agent-harness/`, with ComputeCost and Stats reading the same records. The modules already live in this repository with their original layout, so the platform grows in place; install guides for the kernel, ComputeCost and Stats will be added module by module.

## Development

```bash
cd Relay
npm ci
npm run typecheck
npm test          # vitest; some suites also need Python 3.11+
```

Python modules use their own test folders (`agent-harness/tests_rewrite`, `ComputeCost/tests`, `Stats/exporter/tests`). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

PULSE is released under the [MIT License](LICENSE).

Third-party notices: dependencies are installed from npm and PyPI and are not redistributed in this repository. Most are permissively licensed (`@openai/codex` Apache-2.0; `grammy`, `@larksuiteoapi/node-sdk`, `ws`, `@modelcontextprotocol/sdk` MIT; `node-cron` ISC). `@anthropic-ai/claude-agent-sdk` is distributed by Anthropic under its own commercial terms ("SEE LICENSE IN README.md" in that package). It is an optional dependency used only by the `cc-sdk` runtime. `npm ci` installs it by default, and keeping it installed means accepting those terms; to leave it out, delete `Relay/node_modules/@anthropic-ai/claude-agent-sdk*` after installing. Do not use `npm ci --omit=optional` for this: it also removes the Codex binary.
