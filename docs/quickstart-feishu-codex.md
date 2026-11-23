# Quick start: Relay with Feishu and Codex (`gpt-6.1-sol`)

This guide installs Relay for one person on a fresh server: you message a Feishu bot, Codex answers from a workspace on your server, and only you can use it. It takes about 30 minutes.

## What you need

| Item | Notes |
| --- | --- |
| A Linux server | Ubuntu 24.04 LTS x64, at least 2 vCPU and 4 GB RAM. A US region is recommended so the server can reach OpenAI directly. |
| Node.js 22 LTS | Relay requires Node 18 or newer; 22 is what we run. |
| Codex CLI 0.159 or newer | Older versions reject `gpt-6.1-sol` for ChatGPT sign-in. Relay bundles a newer copy, so you install nothing extra. |
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
sudo apt-get install -y nodejs git bubblewrap
node --version   # v22.x
```

`bubblewrap` is the Codex sandbox on Linux. Ubuntu 24.04 also restricts unprivileged user namespaces through AppArmor, and installing `bubblewrap` alone does not lift that: `bwrap` needs the `bwrap-userns-restrict` AppArmor profile loaded. On 24.04 load it once (from an admin account):

```bash
sudo apt-get install -y apparmor-profiles apparmor-utils
[ -f /etc/apparmor.d/bwrap-userns-restrict ] || \
  sudo cp /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
```

On Ubuntu 25.04 and newer the profile ships in `/etc/apparmor.d/` and is already loaded. Without it every file write inside the workspace fails and check 3 cannot pass; step 2 ends with a sandbox self-test that catches this before you go on.

## 2. Get the code and build

As the `relay` user:

```bash
git clone https://github.com/bigbird-online/pulse-platform.git ~/pulse
cd ~/pulse/Relay
npm ci
npm run build
mkdir -p ~/workspace
```

`npm ci` also installs the Codex CLI for your platform (`@openai/codex` 0.171.0); Relay uses this bundled copy. Check it with `./node_modules/.bin/codex --version`. `gpt-6.1-sol` needs Codex CLI 0.159 or newer: with a ChatGPT sign-in, 0.158 and older fail with `model_not_found` ("not supported when using Codex with a ChatGPT account").

Check that the Codex sandbox can write to the workspace (no sign-in needed):

```bash
cd ~/workspace
~/pulse/Relay/node_modules/.bin/codex sandbox -c 'sandbox_mode="workspace-write"' -- \
  sh -c 'f=$(mktemp ./.sandbox-check.XXXXXX) && rm "$f" && echo sandbox ok'
```

It must print `sandbox ok`. An error that mentions `bwrap`, user namespaces or `RTM_NEWADDR` means the AppArmor profile from step 1 is not loaded.

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
3. Copy the **App ID** and **App Secret** from **Credentials & basic info**.
4. **Permissions & scopes**: under **Permissions & Scopes → API scopes**, add these scopes (app identity):

   | Scope | Name in the console | Used for |
   | --- | --- | --- |
   | `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 (read direct messages sent to the bot) | Receive your messages (`im.message.receive_v1`) |
   | `im:message:send_as_bot` | 以应用的身份发消息 (send messages as the app) | Send replies and update reply cards |
   | `im:message:readonly` | 获取单聊、群组消息 (read direct and group messages) | Read a quoted message and download the images and files you send |
   | `im:message.reactions:write_only` | 发送、删除消息表情回复 (add and remove reactions) | Typing and progress reactions |
   | `im:resource` | 获取与上传图片或文件资源 (read and upload images and files) | Send images and files back |
   | `cardkit:card:write` | 创建与更新卡片 (create and update cards) | Streaming reply cards |
   | `minutes:minutes.transcript:export` | 导出妙记转写的文字内容 (export Minutes transcripts) | Optional: read Feishu Minutes transcripts you share |

   `im:message` (获取与发送单聊、群组消息) covers the second, third and fourth rows at once if you prefer one broader scope. Keep `im:message.p2p_msg:readonly` either way: it is the event scope that lets the bot receive direct messages, and without it pairing and every check after it fail. To use the bot in group chats, also add `im:message.group_at_msg:readonly` (接收群聊中@机器人消息事件) so it receives messages that @-mention it.

Leave **Events & callbacks** for now. Feishu saves a persistent-connection subscription only while an app client is connected, so you configure them in step 6, after Relay is running.

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

### Finish the Feishu app while Relay is running

With Relay connected, go back to the Feishu Open Platform:

1. **Events & callbacks**:
   - Subscription mode: **Receive events through persistent connection** (long connection), then save. If Feishu says no connection was detected, check `journalctl --user -u relay` for a Feishu connection error and that the App ID and App Secret in `relay.json` are right.
   - Add event `im.message.receive_v1` (receive messages).
   - Add callback `card.action.trigger` (card interactions) with the same persistent connection mode.
2. **Version management & release**: create a version, set **Availability** to yourself only, and publish it. Scopes and events take effect only after publishing.

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

For check 4 the other account must be able to reach the bot, and Feishu only lets accounts inside the app's availability range use it. Before the check, add the test account to **Availability**, create and publish a new version, and wait until it is live. After the check, set **Availability** back to yourself only and publish again.

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
| `gpt-6.1-sol` rejected with `model_not_found` | Codex CLI older than 0.159 (`codex --version`), plan level, or workspace admin setting |
| Codex says it cannot write files; `bwrap`, user namespace or `RTM_NEWADDR` errors in its output | Install `bubblewrap` and, on Ubuntu 24.04, load the `bwrap-userns-restrict` AppArmor profile (step 1); rerun the sandbox self-test (step 2) |
| Feishu will not save the persistent-connection subscription | Relay must be running and connected first (step 6) |
| Pairing approval returns `unauthorized` | The `x-relay-token` header must equal `RELAY_INTERNAL_TOKEN` |
| Pairing approval returns `code_unknown` | The code expired after five minutes; send a new message for a new code |
