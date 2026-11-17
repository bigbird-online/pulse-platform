# Quick start: Relay with Feishu and Codex (`gpt-6.1-sol`)

This guide installs Relay for one person on a fresh server: you message a Feishu bot, Codex answers from a workspace on your server, and only you can use it. It takes about 30 minutes.

> Items marked **TODO (verify)** are still being confirmed on a fresh server and will be updated.

## What you need

| Item | Notes |
| --- | --- |
| A Linux server | Ubuntu 24.04 LTS x64, at least 2 vCPU and 4 GB RAM. A US region is recommended so the server can reach OpenAI directly. |
| Node.js 22 LTS | Relay requires Node 18 or newer; 22 is what we run. |
| A ChatGPT account on **Plus or higher** | OpenAI lists `gpt-6.1-sol` for Plus, Pro, Business, Enterprise and Edu plans; Free and Go plans do not include it. Enterprise and Edu workspaces may need an admin to enable it. |
| A Feishu (Lark) tenant where you can create a self-built app | The app connects over a long-lived WebSocket, so the server needs no public IP or domain. |

## 1. Prepare the server

Create a service user and let its user services start at boot:

```bash
sudo adduser --disabled-password --gecos "" relay
sudo loginctl enable-linger relay
sudo -iu relay
```

Install Node.js 22 (any method works; this one uses NodeSource):

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -   # run from an admin account
sudo apt-get install -y nodejs git
node --version   # v22.x
```

## 2. Get the code and build

As the `relay` user:

```bash
git clone https://github.com/bigbird-online/pulse-platform.git ~/pulse
cd ~/pulse/Relay
npm ci
npm run build
mkdir -p ~/workspace
```

`npm ci` also installs the Codex CLI for your platform (`@openai/codex`); Relay uses this bundled copy.

> **TODO (verify):** the bundled Codex CLI is `0.153.4`. Confirm that it accepts `gpt-6.1-sol`; if it reports that a newer version is required, the dependency will be updated.

## 3. Sign in to Codex

1. In ChatGPT, open **Settings → Security** and allow device code sign-in for Codex.
2. On the server:

   ```bash
   ~/pulse/Relay/node_modules/.bin/codex login --device-auth
   ```

   Open the printed URL on your phone or laptop and enter the one-time code.
3. **Check 1 — Codex works on its own:**

   ```bash
   ~/pulse/Relay/node_modules/.bin/codex exec --skip-git-repo-check -m gpt-6.1-sol "Introduce yourself in one sentence"
   ```

   You should see an answer in the terminal. If this fails, fix the Codex login before going on; Relay cannot work without it.

The login is stored in `~/.codex/auth.json`. Treat that file like a password.

## 4. Create the Feishu app

In the [Feishu Open Platform](https://open.feishu.cn/app) (Lark: `open.larksuite.com`):

1. **Create app → Custom app (self-built)**. Give it a name and icon.
2. **Add features → Bot**.
3. **Events & callbacks**:
   - Subscription mode: **Receive events through persistent connection** (long connection).
   - Add event `im.message.receive_v1` (receive messages).
   - Add callback `card.action.trigger` (card interactions) with the same persistent connection mode.
4. **Permissions & scopes**: grant the scopes Relay calls. Relay uses these APIs:

   | API | Used for |
   | --- | --- |
   | `im.message.create`, `im.message.patch`, `im.message.get` | Send, update and read bot messages |
   | `im.messageReaction.create` / `delete` | Typing and progress reactions |
   | `im.messageResource.get` | Download images and files you send |
   | `im.image.create`, `im.file.create` | Send images and files back |
   | `cardkit/v1/cards` | Streaming reply cards |
   | `minutes/v1/minutes/{token}/transcript` | Optional: read Feishu Minutes transcripts you share |

   **TODO (verify):** the exact scope names to tick in the console for these APIs.
5. **Version management & release**: create a version, set **Availability** to yourself only, and publish it.
6. Copy the **App ID** and **App Secret** from **Credentials & basic info**.

## 5. Configure Relay

```bash
mkdir -p ~/.config/relay ~/.local/share/relay && chmod 700 ~/.config/relay
uuidgen   # or: cat /proc/sys/kernel/random/uuid
```

Create `~/.config/relay/relay.json` (mode `0600`):

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
      "feishu": { "appId": "<APP_ID>", "appSecret": "<APP_SECRET>", "sessionId": "<UUID from uuidgen>" }
    }]
  }]
}
```

```bash
chmod 600 ~/.config/relay/relay.json
```

Leave `feishuOpenId` out; pairing in step 7 fills it in.

## 6. Run Relay as a systemd user service

Create a token for the loopback API and an environment file:

```bash
cat > ~/.config/relay/relay.env <<EOF
RELAY_CONFIG_PATH=/home/relay/.config/relay/relay.json
RELAY_DATA_DIR=/home/relay/.local/share/relay
CRON_JOBS_PATH=/home/relay/.config/relay/cron-jobs.json
METERING_DIR=/home/relay/.local/share/relay/metering
RELAY_HTTP_PORT=8788
RELAY_INTERNAL_TOKEN=$(openssl rand -hex 24)
RELAY_CODEX_MODEL=gpt-6.1-sol
RELAY_CODEX_REASONING_EFFORT=medium
EOF
chmod 600 ~/.config/relay/relay.env
```

Do **not** set `PULSE_RELAY_PATHS`: it switches Relay to managed mode, which expects the PULSE work-item kernel.

Create `~/.config/systemd/user/relay.service`:

```ini
[Unit]
Description=PULSE Relay
After=network-online.target

[Service]
EnvironmentFile=/home/relay/.config/relay/relay.env
WorkingDirectory=/home/relay/pulse/Relay
ExecStart=/usr/bin/node /home/relay/pulse/Relay/dist/cli.js
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now relay
journalctl --user -u relay -f
```

The log should show `Relay starting`, `Scheduler loaded 0 jobs` and `Relay HTTP server up 127.0.0.1`.

## 7. Pair your Feishu account

Send any message to the bot in Feishu. It replies with a pairing code, and the log records the same code (`Pairing code issued`). Within five minutes, approve it on the server:

```bash
. ~/.config/relay/relay.env
curl -sS -X POST http://127.0.0.1:8788/internal/pairing/approve \
  -H "content-type: application/json" -H "x-relay-token: $RELAY_INTERNAL_TOKEN" \
  -d '{"code":"<PAIRING CODE>","principalId":"owner","agentId":"assistant"}'
```

`principalId` is the `userId` from `relay.json`. Relay writes your Feishu identity into the config; your next message reaches the assistant.

## 8. Acceptance checks

All five must pass:

| # | Do this | Expected |
| --- | --- | --- |
| 1 | Run `codex exec` in the terminal (step 3) | An answer in the terminal |
| 2 | In Feishu, send "Hello, list the files in your workspace" | A progress card, then a full reply, then "✅ 本轮回复已生成" |
| 3 | Send "Create hello.txt in the workspace with today's date", then run `ls ~/workspace` on the server | `hello.txt` exists |
| 4 | From another account in the same Feishu tenant, message the bot | Only a pairing code; no assistant reply |
| 5 | `sudo reboot`, wait a minute, send another message | The assistant replies as before |

For check 4 the other account must be inside the app's availability range; otherwise Feishu does not deliver the message at all, which is also a pass.

## Everyday commands

| Command | Effect |
| --- | --- |
| `/new` | Start a new conversation |
| `/stop` | Stop the current turn |
| `/status` | Show the session, runtime and model |
| `/model` | Switch the model |
| `/help` | List commands |

Put an `AGENTS.md` in the workspace to give the assistant standing instructions; Codex reads it at the start of each conversation.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No reply and no pairing code | `journalctl --user -u relay`; confirm the app version is published and the event uses the persistent connection |
| Terminal `codex exec` fails | Codex login (step 3) or plan level; Relay cannot fix this |
| `gpt-6.1-sol` rejected | Plan level, workspace admin setting, or Codex CLI version (see the TODO in step 2) |
| Pairing approval returns `unauthorized` | The `x-relay-token` header must equal `RELAY_INTERNAL_TOKEN` |
| Pairing approval returns `code_unknown` | The code expired after five minutes; send a new message for a new code |
