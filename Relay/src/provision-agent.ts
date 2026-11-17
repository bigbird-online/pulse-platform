#!/usr/bin/env node
// CLI: provision-agent — add a new Agent to a Relay config.
// 默认路径由 config-paths.ts 的 resolveConfigPath() 单一决定，本文件不再自带兜底。
//
// canonical entry compiled to dist/provision-agent.js; scripts/provision-agent.ts is a
// tsx compatibility shim (mirrors relay-jobs packaging decision, see
// pulse-space/memo/decision-2026-05-01-relay-jobs-packaging.md).

import { pathToFileURL } from 'node:url';
import { provisionAgent, ProvisionError, type ProvisionRequest } from './provision.js';
import { resolveConfigPath } from './config-paths.js';

// 曾在此写死 `$HOME/.relay/relay.json`，与 index.ts / relay-jobs.ts 的默认值互不一致。
// 现走共享真源 resolveConfigPath()——注意默认值已从「运行态目录」改为「仓内 data/」，
// 详见 config-paths.ts 顶部的选值理由。
function defaultConfigPath(): string {
  return resolveConfigPath();
}

function parseArgs(argv: string[]): Record<string, string | boolean> {
  const result: Record<string, string | boolean> = {};
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (!token.startsWith('--')) continue;
    const key = token.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) {
      result[key] = true;
    } else {
      result[key] = next;
      i++;
    }
  }
  return result;
}

function usage(printedExplicitly = false): number {
  console.log(`用法:
  provision-agent --user <userId> --id <agentId> --name <name> --emoji <emoji> \
                  --workspace <path> [--bot-token <token>] \
                  [--channel telegram|feishu] \
                  [--feishu-app-id <id>] [--feishu-app-secret <secret>] [--feishu-open-id <openId>] \
                  [--monthly-budget <usd>] [--max-turns <n>] [--allowed-tools <a,b,c>] \
                  [--runtime cc|codex] [--config <path>] [--dry-run]

默认 config 路径: \$RELAY_CONFIG_PATH 或 ${defaultConfigPath()}
写前自动备份到 <config-dir>/backups/relay-<ISO ts>.json

示例:
  # TG agent
  provision-agent --user owner --id testbot --name TestBot --emoji 🧪 \
                  --workspace /tmp/test --bot-token 123:abc

  # Feishu agent
  provision-agent --user eric --id helper --name Helper --emoji 💡 \
                  --workspace /tmp/helper --channel feishu \
                  --feishu-app-id cli_x --feishu-app-secret secret_y --feishu-open-id ou_z

  # 带软配额
  provision-agent --user owner --id steward --name Steward --emoji 🪙 \
                  --workspace /tmp/steward --bot-token 456:def --monthly-budget 25
`);
  return printedExplicitly ? 0 : 1;
}

function asNumber(value: string | boolean | undefined): number | undefined {
  if (typeof value !== 'string') return undefined;
  const num = Number(value);
  return Number.isFinite(num) ? num : undefined;
}

function asString(value: string | boolean | undefined): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  if (argv.length === 0) return usage(false);
  if (argv.includes('--help') || argv.includes('-h')) return usage(true);

  const args = parseArgs(argv);

  const userId = asString(args['user']);
  const agentId = asString(args['id']);
  const name = asString(args['name']);
  const emoji = asString(args['emoji']);
  const workspace = asString(args['workspace']);

  if (!userId || !agentId || !name || !emoji || !workspace) {
    console.error('错误: --user / --id / --name / --emoji / --workspace 全部为必填。');
    return usage();
  }

  const channelRaw = asString(args['channel']);
  const channel = channelRaw === 'telegram' || channelRaw === 'feishu' ? channelRaw : undefined;
  const runtimeRaw = asString(args['runtime']);
  const runtime = runtimeRaw === 'cc' || runtimeRaw === 'codex' ? runtimeRaw : undefined;
  const allowedToolsRaw = asString(args['allowed-tools']);
  const allowedTools = allowedToolsRaw ? allowedToolsRaw.split(',').map((t) => t.trim()).filter(Boolean) : undefined;

  const request: ProvisionRequest = {
    configPath: asString(args['config']) ?? defaultConfigPath(),
    userId,
    agentId,
    name,
    emoji,
    workspace,
    channel,
    botToken: asString(args['bot-token']),
    feishuAppId: asString(args['feishu-app-id']),
    feishuAppSecret: asString(args['feishu-app-secret']),
    feishuOpenId: asString(args['feishu-open-id']),
    monthlyBudgetUsd: asNumber(args['monthly-budget']),
    maxTurns: asNumber(args['max-turns']),
    allowedTools,
    runtime,
    dryRun: Boolean(args['dry-run']),
  };

  try {
    const outcome = provisionAgent(request);
    console.log(`✅ Agent 已${outcome.dryRun ? '预演（dry-run，未写盘）' : '创建'}:`);
    console.log(`   user        : ${outcome.user}`);
    console.log(`   agent       : ${outcome.agent.id} (${outcome.agent.name} ${outcome.agent.emoji})`);
    console.log(`   workspace   : ${outcome.agent.workspace}`);
    console.log(`   channel     : ${outcome.agent.channel ?? 'telegram (default)'}`);
    if (outcome.agent.sessionId) console.log(`   sessionId   : ${outcome.agent.sessionId}`);
    if (outcome.agent.feishu?.sessionId) console.log(`   feishu sess : ${outcome.agent.feishu.sessionId}`);
    if (outcome.agent.monthlyBudgetUsd) console.log(`   budget      : $${outcome.agent.monthlyBudgetUsd}/month`);
    console.log(`   config      : ${outcome.configPath}`);
    if (outcome.backupPath) console.log(`   backup      : ${outcome.backupPath}`);
    if (!outcome.dryRun) {
      console.log('\n下一步:');
      console.log('  - 重启 Relay 让新 agent 生效（macOS: launchctl kickstart -k gui/$UID/ai.pulse.relay）');
      console.log('  - 在通道上 /list 验证；首条消息会触发 spawn');
    }
    return 0;
  } catch (err) {
    if (err instanceof ProvisionError) {
      console.error(`❌ ${err.code}: ${err.message}`);
      return 1;
    }
    console.error(err);
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    process.exitCode = await main();
  } catch (err) {
    console.error(err);
    process.exitCode = 1;
  }
}
