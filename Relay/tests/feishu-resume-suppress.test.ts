import { describe, it, expect, beforeEach } from 'vitest';
import { _shouldEmitFeishuResume, _resetFeishuResumeSuppressState } from '../src/channels/feishu.js';

const STORM_MS = 3 * 60 * 1000;

describe('Feishu resume same-storm suppression', () => {
  beforeEach(() => {
    _resetFeishuResumeSuppressState();
  });

  it('first call per openId emits', () => {
    expect(_shouldEmitFeishuResume('openA', 1_000_000)).toBe(true);
  });

  it('second call within storm window suppresses', () => {
    expect(_shouldEmitFeishuResume('openA', 1_000_000)).toBe(true);
    expect(_shouldEmitFeishuResume('openA', 1_000_000 + 1_000)).toBe(false);
    expect(_shouldEmitFeishuResume('openA', 1_000_000 + STORM_MS - 1)).toBe(false);
  });

  it('call past storm window emits again', () => {
    expect(_shouldEmitFeishuResume('openA', 1_000_000)).toBe(true);
    expect(_shouldEmitFeishuResume('openA', 1_000_000 + STORM_MS + 1)).toBe(true);
  });

  it('different openIds are independent', () => {
    expect(_shouldEmitFeishuResume('openA', 1_000_000)).toBe(true);
    expect(_shouldEmitFeishuResume('openB', 1_000_000 + 1)).toBe(true);
    // Both should still be suppressed individually right after
    expect(_shouldEmitFeishuResume('openA', 1_000_000 + 2)).toBe(false);
    expect(_shouldEmitFeishuResume('openB', 1_000_000 + 3)).toBe(false);
  });

  it('reset clears all state', () => {
    expect(_shouldEmitFeishuResume('openA', 1_000_000)).toBe(true);
    _resetFeishuResumeSuppressState();
    // After in-memory reset (which is what happens on real Relay process restart),
    // the next restart wave will notify again — this matches user expectation that
    // a genuine restart should produce a wake-up, not silence forever.
    expect(_shouldEmitFeishuResume('openA', 1_000_000 + 100)).toBe(true);
  });
});
