/** A Relay-originated delivery is marked for ComputeCost, never as a human input. */
import { describe, it, expect, afterAll } from 'vitest';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { collaborationRef, internalDeliveryPrompt, INTERNAL_DELIVERY_POLICY } from '../src/collaboration-capture.js';

const spool = mkdtempSync(join(tmpdir(), 'relay-internal-delivery-'));
afterAll(() => rmSync(spool, { recursive: true, force: true }));
const SESSION = '4c5a7407-ff1e-4e7a-b51b-9d8d61bc386c';
const AT = new Date('2026-10-26T05:07:37.000Z');

describe('internalDeliveryPrompt', () => {
  it('binds a one-off reference in the prompt to the native session in the Host spool', () => {
    const prompt = internalDeliveryPrompt('[系统投递：协作合同答复，不是人类消息]\n…', 'cc', SESSION,
      { PULSE_COMPUTECOST_RECEIPT_DIR: spool }, () => AT);
    const match = /\n\n\[Host internal delivery reference: ([a-f0-9]{64})\. Relay delivery, not a human message\.\]$/.exec(prompt);
    expect(match).not.toBeNull();
    expect(prompt).not.toContain('Host human input reference');
    const events = readdirSync(spool).flatMap(name => readFileSync(join(spool, name), 'utf8')
      .split('\n').filter(Boolean).map(line => JSON.parse(line)));
    expect(events).toHaveLength(1);
    const ref = collaborationRef('internal_delivery', [match![1], collaborationRef('native_session', ['cc', SESSION])]);
    expect(events[0]).toMatchObject({ kind: 'attribution', target_ref: ref, policy: INTERNAL_DELIVERY_POLICY,
      state: 'unassigned', wi: null, executor: null, effective_at: AT.toISOString(), at: AT.toISOString() });
    expect(JSON.stringify(events[0])).not.toContain(SESSION);
    // Each delivery has its own reference.
    const again = internalDeliveryPrompt('x', 'cc', SESSION, { PULSE_COMPUTECOST_RECEIPT_DIR: spool }, () => AT);
    expect(again.slice(-90)).not.toEqual(prompt.slice(-90));
  });

  it('leaves the prompt unchanged without a Host spool or native session', () => {
    expect(internalDeliveryPrompt('x', 'cc', SESSION, {})).toBe('x');
    expect(internalDeliveryPrompt('x', 'cc', '', { PULSE_COMPUTECOST_RECEIPT_DIR: spool })).toBe('x');
  });
});
