#!/usr/bin/env node

import { mkdirSync, openSync, closeSync, existsSync, fsyncSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  loadNodePlacement,
  projectNodeConfig,
  readCronSource,
  readRelaySource,
  stableJson,
  stageNodePlacedJob,
  validateSourceInventories,
  type NodePlacementDocument,
} from './node-placement.js';
import type { ScheduledJob } from './types.js';

function args(argv: string[]): { command?: string; values: Record<string, string>; flags: Set<string> } {
  const command = argv[0];
  const values: Record<string, string> = {};
  const flags = new Set<string>();
  for (let index = 1; index < argv.length; index++) {
    const token = argv[index];
    if (!token.startsWith('--')) throw new Error(`unexpected argument: ${token}`);
    const name = token.slice(2);
    const next = argv[index + 1];
    if (next && !next.startsWith('--')) {
      values[name] = next;
      index++;
    } else {
      flags.add(name);
    }
  }
  return { command, values, flags };
}

function required(values: Record<string, string>, name: string): string {
  const value = values[name];
  if (!value) throw new Error(`missing --${name}`);
  return value;
}

function usage(): number {
  console.error(`Usage:
  node-placement validate --ssot <path> [--relay-source <path> --cron-source <path>]
  node-placement project --ssot <path> --current-node <id> --relay-source <path> --cron-source <path> --relay-out <path> --cron-out <path> [--allow-candidate]
  node-placement preflight --ssot <path> --current-node <id> --relay-source <path> --cron-source <path> --relay-local <path> --cron-local <path> [--allow-candidate]
  node-placement stage-add-job --ssot <path> --current-node <id> --relay-source <path> --cron-source <path> --job <path> --ssot-out <new-path> --cron-source-out <new-path> [--allow-candidate]

The CLI never prints config content or credential values; success output contains counts and SHA-256 only.`);
  return 2;
}

function assertProjectable(placement: NodePlacementDocument, allowCandidate: boolean): void {
  if (placement.status !== 'active' && !allowCandidate) {
    throw new Error(`placement ${placement.deploymentId} is ${placement.status}; pass --allow-candidate only for isolated preview`);
  }
}

function writeStaged(path: string, bytes: string): void {
  if (existsSync(path)) throw new Error(`refusing to replace existing file from project command: ${path}`);
  mkdirSync(dirname(path), { recursive: true });
  const temp = `${path}.tmp-${process.pid}`;
  writeFileSync(temp, bytes, { mode: 0o600 });
  const fd = openSync(temp, 'r');
  try {
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temp, path);
}

export async function main(argv = process.argv.slice(2)): Promise<number> {
  try {
    const parsed = args(argv);
    if (!parsed.command) return usage();
    const placement = loadNodePlacement(required(parsed.values, 'ssot'));

    if (parsed.command === 'validate') {
      const relayPath = parsed.values['relay-source'];
      const cronPath = parsed.values['cron-source'];
      if (Boolean(relayPath) !== Boolean(cronPath)) throw new Error('--relay-source and --cron-source must be provided together');
      if (relayPath && cronPath) validateSourceInventories(placement, readRelaySource(relayPath), readCronSource(cronPath));
      console.log(stableJson({ ok: true, deploymentId: placement.deploymentId, status: placement.status, nodes: placement.nodes.length, agents: placement.agents.length, bots: placement.bots.length, jobs: placement.jobs.length }).trim());
      return 0;
    }

    if (parsed.command === 'stage-add-job') {
      assertProjectable(placement, parsed.flags.has('allow-candidate'));
      const relaySource = readRelaySource(required(parsed.values, 'relay-source'));
      const cronSource = readCronSource(required(parsed.values, 'cron-source'));
      let rawJob: Omit<ScheduledJob, '_state'>;
      try {
        rawJob = JSON.parse(readFileSync(required(parsed.values, 'job'), 'utf8')) as Omit<ScheduledJob, '_state'>;
      } catch (error) {
        throw new Error(`cannot read staged job: ${String(error)}`);
      }
      const staged = stageNodePlacedJob(placement, required(parsed.values, 'current-node'), cronSource, rawJob);
      validateSourceInventories(staged.placement, relaySource, staged.cronSource);
      const ssotOut = required(parsed.values, 'ssot-out');
      const cronOut = required(parsed.values, 'cron-source-out');
      if (existsSync(ssotOut) || existsSync(cronOut)) throw new Error('stage-add-job outputs must both be new staging paths');
      writeStaged(ssotOut, stableJson(staged.placement));
      writeStaged(cronOut, stableJson(staged.cronSource));
      console.log(stableJson({ ok: true, jobId: staged.job.id, ownerId: staged.job.owner, runNode: staged.job.runNode }).trim());
      return 0;
    }

    if (parsed.command !== 'project' && parsed.command !== 'preflight') return usage();
    assertProjectable(placement, parsed.flags.has('allow-candidate'));
    const projected = projectNodeConfig(
      placement,
      required(parsed.values, 'current-node'),
      readRelaySource(required(parsed.values, 'relay-source')),
      readCronSource(required(parsed.values, 'cron-source')),
    );

    if (parsed.command === 'project') {
      // project is deliberately staging-only. Live replacement needs the
      // runbook's snapshot + negative-writer evidence and is never hidden in
      // this command.
      const relayOut = required(parsed.values, 'relay-out');
      const cronOut = required(parsed.values, 'cron-out');
      if (existsSync(relayOut) || existsSync(cronOut)) {
        throw new Error('project outputs must both be new staging paths');
      }
      writeStaged(relayOut, projected.relayBytes);
      writeStaged(cronOut, projected.cronBytes);
      console.log(stableJson({ ok: true, relaySha256: projected.relaySha256, cronSha256: projected.cronSha256, relayUsers: projected.relay.users.length, cronJobs: projected.cron.jobs.length }).trim());
      return 0;
    }

    const relayLocal = stableJson(readRelaySource(required(parsed.values, 'relay-local')));
    const cronLocal = stableJson(readCronSource(required(parsed.values, 'cron-local')));
    if (relayLocal !== projected.relayBytes) throw new Error(`relay projection mismatch: expected sha256:${projected.relaySha256}`);
    if (cronLocal !== projected.cronBytes) throw new Error(`cron projection mismatch: expected sha256:${projected.cronSha256}`);
    console.log(stableJson({ ok: true, relaySha256: projected.relaySha256, cronSha256: projected.cronSha256 }).trim());
    return 0;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main();
}
