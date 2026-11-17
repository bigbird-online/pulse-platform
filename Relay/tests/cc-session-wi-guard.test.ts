import { describe, expect, it } from 'vitest';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import {
  DECLARE_TOOL, WORK_ITEM_TOOL, guardedOperation, mainTranscriptPath, preToolUseDecision, requestDeclarationState,
} from '../runtime/cc-session-wi-guard.mjs';
import { piGuardedOperation } from '../runtime/pi-work-item-core.mjs';
import {
  GUARD_MATCHER, SESSION_WI_REMINDER, addSessionWiGuardSettings, codexSessionDeclared, withSessionWiReminder,
} from '../src/session-wi-guard.js';

const GUARD = resolve('runtime/cc-session-wi-guard.mjs');
const ACK = (wi: string) => JSON.stringify({ origin: 'agent_declaration', schema: 'cc_request_wi/v1', wi });

const request = (promptId: string, text = '做事') =>
  ({ type: 'user', isSidechain: false, promptId, message: { role: 'user', content: text } });
const declareUse = (id: string, wi = 'wi_20261015_422') =>
  ({ type: 'assistant', isSidechain: false, message: { content: [{ type: 'tool_use', id, name: DECLARE_TOOL, input: { wi } }] } });
const declareResult = (id: string, promptId: string, wi = 'wi_20261015_422', extra: Record<string, unknown> = {}) =>
  ({ type: 'user', isSidechain: false, promptId,
    message: { content: [{ type: 'tool_result', tool_use_id: id, content: ACK(wi), ...extra }] } });
const transcript = (...rows: unknown[]) => rows.map(row => JSON.stringify(row)).join('\n') + '\n';
const bash = (command: string) => ({ tool_name: 'Bash', tool_input: { command } });

describe('CC request declaration state', () => {
  it('follows the latest human request and its own declaration', () => {
    expect(requestDeclarationState(transcript(request('p1')))).toBe('undeclared');
    expect(requestDeclarationState(transcript(request('p1'), declareUse('u1'), declareResult('u1', 'p1')))).toBe('declared');
    expect(requestDeclarationState(transcript(request('p1'), declareUse('u1'), declareResult('u1', 'p1'), request('p2'))))
      .toBe('undeclared');
    expect(requestDeclarationState('')).toBe('unknown');
  });

  it('does not count refused, foreign-prompt or subagent declarations', () => {
    expect(requestDeclarationState(transcript(request('p1'), declareUse('u1'),
      declareResult('u1', 'p1', 'wi_20261015_422', { is_error: true })))).toBe('undeclared');
    expect(requestDeclarationState(transcript(request('p1'), declareUse('u1'), declareResult('u1', 'p0')))).toBe('undeclared');
    expect(requestDeclarationState(transcript(request('p1'), { ...declareUse('u1'), isSidechain: true },
      { ...declareResult('u1', 'p1'), isSidechain: true }))).toBe('undeclared');
  });

  it('keeps the request across interrupts and task notifications', () => {
    const declared = [request('p1'), declareUse('u1'), declareResult('u1', 'p1')];
    expect(requestDeclarationState(transcript(...declared, request('p1', '[Request interrupted by user]')))).toBe('declared');
    expect(requestDeclarationState(transcript(...declared,
      { ...request('p9', '<task-notification><task-id>x</task-id></task-notification>') }))).toBe('declared');
  });
});

describe('CC write-operation guard', () => {
  const undeclared = transcript(request('p1'));
  const declared = transcript(request('p1'), declareUse('u1'), declareResult('u1', 'p1'));
  const decide = (input: object, body: string) => preToolUseDecision({ transcript_path: '/t/s.jsonl', ...input }, () => body);

  it.each([
    'git commit -m "fix: x"', 'git -C /repo push origin HEAD', 'cd /repo && git push', 'bash -c "cd x && git push"',
    'gh pr merge 12 --squash', '/py -I -B Scripts/pulse-gh.py --transport local run -- pr merge 12',
    'launchctl bootout gui/501/com.x', 'sudo launchctl bootstrap gui/501 /p.plist', 'FOO=1 git commit -am x',
  ])('denies %s before the request is declared and allows it after', (command) => {
    const denied = decide(bash(command), undeclared);
    expect(denied?.hookSpecificOutput.permissionDecision).toBe('deny');
    expect(denied?.hookSpecificOutput.permissionDecisionReason).toContain(DECLARE_TOOL);
    expect(decide(bash(command), declared)).toBeUndefined();
  });

  it('keeps a request declared none read-only until it declares a WI', () => {
    const none = transcript(request('p1'), declareUse('u1', 'none'), declareResult('u1', 'p1', 'none'));
    expect(requestDeclarationState(none)).toBe('none');
    const denied = decide(bash('git push'), none);
    expect(denied?.hookSpecificOutput.permissionDecision).toBe('deny');
    expect(denied?.hookSpecificOutput.permissionDecisionReason).toContain('本请求声明为 none');
    expect(decide(bash('git status'), none)).toBeUndefined();
    const then = transcript(request('p1'), declareUse('u1', 'none'), declareResult('u1', 'p1', 'none'),
      declareUse('u2'), declareResult('u2', 'p1'));
    expect(requestDeclarationState(then)).toBe('declared');
    expect(decide(bash('git push'), then)).toBeUndefined();
  });

  it.each(['git status', 'git log --grep push', 'echo "git push"', 'gh pr view 12', 'launchctl print gui/501', 'ls'])(
    'lets read-only %s through undeclared', (command) => {
      expect(decide(bash(command), undeclared)).toBeUndefined();
    });

  it('guards only the work-item execute action and never the declaration itself', () => {
    expect(decide({ tool_name: WORK_ITEM_TOOL, tool_input: { action: 'execute' } }, undeclared)).toBeDefined();
    expect(decide({ tool_name: WORK_ITEM_TOOL, tool_input: { action: 'inspect' } }, undeclared)).toBeUndefined();
    expect(decide({ tool_name: DECLARE_TOOL, tool_input: { wi: 'wi_20261015_422' } }, undeclared)).toBeUndefined();
    expect(guardedOperation('Read', { file_path: '/x' })).toBeUndefined();
    expect(GUARD_MATCHER.split('|')).toEqual(['Bash', WORK_ITEM_TOOL]);
  });

  it.each(['create_draft', 'create_lightweight'])('lets %s create the WI the request then declares', (intent) => {
    expect(decide({ tool_name: WORK_ITEM_TOOL, tool_input: { action: 'execute', intent } }, undeclared)).toBeUndefined();
    expect(decide({ tool_name: WORK_ITEM_TOOL, tool_input: { action: 'execute', intent: 'start_work' } }, undeclared))
      .toBeDefined();
    expect(piGuardedOperation('pulse_work_item', { action: 'execute', intent })).toBeUndefined();
    expect(piGuardedOperation('pulse_work_item', { action: 'execute', intent: 'start_work' })).toBe('pulse_work_item execute');
  });

  it('lets work proceed when the transcript is unreadable or has no request', () => {
    expect(preToolUseDecision({ transcript_path: '/t/s.jsonl', ...bash('git push') }, () => { throw new Error('gone'); }))
      .toBeUndefined();
    expect(decide(bash('git push'), '')).toBeUndefined();
  });

  it('judges a subagent call by the main session transcript', () => {
    expect(mainTranscriptPath('/p/sid/subagents/agent-1.jsonl')).toBe('/p/sid.jsonl');
    expect(mainTranscriptPath('/p/sid.jsonl')).toBe('/p/sid.jsonl');
  });

  it('runs as a CC hook command', () => {
    const dir = mkdtempSync(join(tmpdir(), 'wi-guard-hook-'));
    try {
      const path = join(dir, 'sid.jsonl');
      writeFileSync(path, undeclared);
      const run = (input: object) => execFileSync(process.execPath, [GUARD], { input: JSON.stringify(input), encoding: 'utf8' });
      expect(JSON.parse(run({ transcript_path: path, ...bash('git push') })).hookSpecificOutput.permissionDecision).toBe('deny');
      writeFileSync(path, declared);
      expect(run({ transcript_path: path, ...bash('git push') })).toBe('');
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
});

describe('reminder and settings helpers', () => {
  it('appends the reminder to text and block prompts but not to slash commands', () => {
    expect(withSessionWiReminder({ type: 'human', content: 'hi' }).content).toBe(`hi\n\n${SESSION_WI_REMINDER}`);
    expect(withSessionWiReminder({ type: 'human', content: [{ type: 'text', text: 'hi' }] }).content)
      .toEqual([{ type: 'text', text: 'hi' }, { type: 'text', text: SESSION_WI_REMINDER }]);
    expect(withSessionWiReminder({ type: 'human', content: '/compact' }).content).toBe('/compact');
  });

  it('merges the hook into the single CC --settings value', () => {
    const inline = ['-p', '--settings', JSON.stringify({ apiKeyHelper: '', hooks: { Stop: [{ hooks: [] }] } })];
    addSessionWiGuardSettings(inline, '/node');
    const settings = JSON.parse(inline[2]);
    expect(inline.filter(arg => arg === '--settings')).toHaveLength(1);
    expect(settings.apiKeyHelper).toBe('');
    expect(settings.hooks.Stop).toHaveLength(1);
    expect(settings.hooks.PreToolUse[0].matcher).toBe(GUARD_MATCHER);
    expect(settings.hooks.PreToolUse[0].hooks[0].command).toBe(`'/node' '${GUARD}'`);
    const none = ['-p'];
    addSessionWiGuardSettings(none, '/node');
    expect(JSON.parse(none[2]).hooks.PreToolUse).toHaveLength(1);
  });

  it('counts only a completed successful Codex declaration', () => {
    const row = (status: string, isError: boolean) => JSON.stringify({ type: 'event_msg', payload: { type: 'item_completed',
      item: { type: 'McpToolCall', server: 'pulse-request-purpose', tool: 'declare_session_wi', status, result: { isError } } } });
    expect(codexSessionDeclared(row('completed', false) + '\n')).toBe(true);
    expect(codexSessionDeclared(row('completed', true) + '\n')).toBe(false);
    expect(codexSessionDeclared(row('failed', false) + '\n')).toBe(false);
    expect(codexSessionDeclared(undefined)).toBe(false);
  });
});
