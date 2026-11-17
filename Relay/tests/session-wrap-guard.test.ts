import { describe, expect, it } from 'vitest';
import { hasWrapBlockerCode, hasWrapEvidence } from '../src/session-wrap-guard.js';

describe('session wrap guard', () => {
  it('recognizes /wrap evidence', () => {
    expect(hasWrapEvidence('/wrap')).toBe(true);
    expect(hasWrapEvidence('执行 /wrap')).toBe(true);
    expect(hasWrapEvidence('我想了解 /wrap 的流程')).toBe(false);
    expect(hasWrapEvidence('路径 ssot/skills/ops/wrap/SKILL.md 在哪里？')).toBe(false);
  });

  it('recognizes only explicit wrap blocker codes', () => {
    expect(hasWrapBlockerCode('BLOCKED_WRAP_ARCHIVE_AUTHORITY_UNAVAILABLE')).toBe(true);
    expect(hasWrapBlockerCode('BLOCKED_TRANSCRIPT_MISSING')).toBe(true);
    expect(hasWrapBlockerCode('BLOCKED_COMPUTE_PROFILE_RESOLUTION')).toBe(true);
    expect(hasWrapBlockerCode('收口完成，准备 /new')).toBe(false);
    expect(hasWrapBlockerCode('BLOCKED_WORK_ITEM_AUTHORITY')).toBe(false);
  });
});
