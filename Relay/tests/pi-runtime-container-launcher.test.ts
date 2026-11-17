import { execFile } from 'node:child_process';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { promisify } from 'node:util';
import { afterEach, describe, expect, it } from 'vitest';
import { PI_RUNTIME_COMMIT, PI_RUNTIME_VERSION } from './pi-runtime-fixture.js';

const execFileAsync = promisify(execFile);
const roots: string[] = [];
const launcher = resolve('scripts/pi-runtime-container-launcher.sh');
const dockerfile = resolve('runtime/pi/Dockerfile');

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function fakeDocker(revision = PI_RUNTIME_COMMIT, engineName = 'docker'): {
  root: string;
  engine: string;
  workspace: string;
  sessions: string;
  resource: string;
  log: string;
} {
  const root = mkdtempSync(join(tmpdir(), 'pi-launcher-'));
  roots.push(root);
  const engine = join(root, engineName);
  const workspace = join(root, 'workspace');
  const sessions = join(root, 'sessions');
  const resource = join(workspace, 'extension.ts');
  const log = join(root, 'docker.log');
  mkdirSync(workspace);
  mkdirSync(sessions);
  writeFileSync(resource, 'export default {};\n');
  writeFileSync(engine, `#!/bin/bash
set -euo pipefail
if [[ \${1-} == image && \${2-} == inspect ]]; then
  printf '%s\\n' '${revision}'
  exit 0
fi
if [[ \${*-} == *' --version'* ]]; then
  printf 'pi ${PI_RUNTIME_VERSION}\\n'
  exit 0
fi
printf 'host-home=%s\\n' "$HOME" >> "$FAKE_DOCKER_LOG"
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
`);
  chmodSync(engine, 0o755);
  return { root, engine, workspace, sessions, resource, log };
}

function args(f: ReturnType<typeof fakeDocker>): string[] {
  return [
    '--engine', f.engine,
    '--engine-home', f.root,
    '--image', 'registry.internal/pi@sha256:abcdef',
    '--user', '65532:65532',
    '--network', 'bridge',
    '--workspace', f.workspace,
    '--session-dir', f.sessions,
    '--isolation', 'container',
    '--expected-pi-version', PI_RUNTIME_VERSION,
    '--expected-pi-commit', PI_RUNTIME_COMMIT,
    '--env-key', 'ANTHROPIC_API_KEY',
    '--',
    '/opt/pi/bin/pi', '--mode', 'rpc', '--extension', f.resource,
  ];
}

describe('Pi container isolation launcher', () => {
  it('executes only the pinned, hardened container path', async () => {
    const f = fakeDocker();
    await execFileAsync(launcher, args(f), {
      env: { FAKE_DOCKER_LOG: f.log, ANTHROPIC_API_KEY: 'secret', SCL_SESSION_REF: `session_v1_${'a'.repeat(64)}`, HOME: f.sessions },
    });
    const invocation = readFileSync(f.log, 'utf8');
    expect(invocation).toContain(`host-home=${f.root}`);
    expect(invocation).toContain('run --rm --init --interactive --read-only');
    expect(invocation).toContain('--cap-drop ALL');
    expect(invocation).toContain('--security-opt no-new-privileges');
    expect(invocation).toContain('--network bridge');
    expect(invocation).toContain('--user 65532:65532');
    expect(invocation).toContain(`--volume ${f.workspace}:${f.workspace}:rw`);
    expect(invocation).toContain(`--volume ${f.resource}:${f.resource}:ro`);
    expect(invocation).toContain('--env ANTHROPIC_API_KEY');
    expect(invocation).toContain('--env SCL_SESSION_REF');
    expect(invocation).toContain(`--env HOME=${f.sessions}`);
    expect(invocation).not.toContain('secret');
    expect(invocation).toContain('/opt/pi/bin/pi --mode rpc');
  });

  it('builds from digest, npm integrity, git commit, and version pins', () => {
    const source = readFileSync(dockerfile, 'utf8');
    expect(source).toMatch(/^FROM node:22-bookworm-slim@sha256:[0-9a-f]{64}$/m);
    expect(source).toContain(`ARG PI_VERSION=${PI_RUNTIME_VERSION}`);
    expect(source).toContain(`ARG PI_COMMIT=${PI_RUNTIME_COMMIT}`);
    expect(source).toMatch(/ARG PI_TARBALL_SHA512=[0-9a-f]{128}/);
    expect(source).toContain('npm view "@earendil-works/pi-coding-agent@${PI_VERSION}" gitHead');
    expect(source).toContain('sha512sum -c -');
    expect(source).toContain('USER 65532:65532');
  });

  it('fails closed before execution when the image revision is wrong', async () => {
    const f = fakeDocker('0000000000000000000000000000000000000000');
    await expect(execFileAsync(launcher, args(f), {
      env: { FAKE_DOCKER_LOG: f.log, HOME: f.sessions },
    })).rejects.toMatchObject({ code: 64 });
    expect(() => readFileSync(f.log, 'utf8')).toThrow();
  });

  it('keeps the Relay uid/gid mapping for rootless Podman mounts', async () => {
    const f = fakeDocker(PI_RUNTIME_COMMIT, 'podman');
    await execFileAsync(launcher, args(f), {
      env: { FAKE_DOCKER_LOG: f.log, SCL_SESSION_REF: `session_v1_${'a'.repeat(64)}`, HOME: f.sessions },
    });
    expect(readFileSync(f.log, 'utf8')).toContain('--userns keep-id');
  });
});
