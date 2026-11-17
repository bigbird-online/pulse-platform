import { describe, expect, it } from 'vitest';
import { withSystemPath } from '../src/pool.js';

describe('withSystemPath', () => {
  it('appends missing system directories after the launchd PATH', () => {
    expect(withSystemPath('/opt/node/bin:/usr/local/bin:/usr/bin:/bin'))
      .toBe('/opt/node/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin');
  });

  it('keeps an existing order and does not duplicate directories', () => {
    expect(withSystemPath('/usr/sbin:/custom:/usr/bin:/bin:/sbin')).toBe('/usr/sbin:/custom:/usr/bin:/bin:/sbin');
  });

  it('provides the system directories when PATH is absent', () => {
    expect(withSystemPath(undefined)).toBe('/usr/bin:/bin:/usr/sbin:/sbin');
  });
});
