import { execFileSync } from 'node:child_process';
import { userInfo } from 'node:os';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync, symlinkSync, mkdirSync, realpathSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { validateConfig } from '../src/types.js';
import { telegramTextWithLinks, telegramFileLinks, readTelegramLocalFiles } from '../src/channels/telegram-links.js';

describe('Telegram link input', () => {
  it('rejects a filesystem-root read grant in user configuration', () => {
    for (const root of ['/', '/tmp/..', 'relative']) {
      expect(() => validateConfig({ users: [{ userId: 'u', defaultAgent: 'a', agents: [], telegramLocalFileRoots: [root] }] }))
        .toThrow('absolute, non-root');
    }
  });
  it('keeps visible text, plain URLs and hidden targets using Telegram UTF-16 offsets', () => {
    const text = '🦜 看图和原文 https://example.org/plain';
    expect(telegramTextWithLinks(text, [
      { type: 'text_link', offset: 3, length: 2, url: 'https://example.org/image.png' },
      { type: 'text_link', offset: 6, length: 2, url: 'https://example.org/article' },
    ])).toBe('🦜 看图 (https://example.org/image.png)和原文 (https://example.org/article) https://example.org/plain');
    expect(telegramTextWithLinks(text, [{ type: 'text_link', offset: -1, length: 8, url: 'bad' }])).toBe(text);
  });
  it('reads the exact decoded file and fragment without widening another user’s access', () => {
    const dir = realpathSync(mkdtempSync(join(tmpdir(), 'relay-link-test-')));
    try {
      const file = join(dir, 'page with spaces.html');
      writeFileSync(file, '<h1>unique page body</h1>');
      const url = pathToFileURL(file).href + '#1';
      expect(readTelegramLocalFiles([url], [dir])[0].bytes.toString()).toContain('unique page body');
      expect(() => readTelegramLocalFiles([url])).toThrow('接收目录');
      const allowed = join(dir, 'allowed'); mkdirSync(allowed);
      symlinkSync(file, join(allowed, 'escape.html'));
      expect(() => readTelegramLocalFiles([pathToFileURL(join(allowed, 'escape.html')).href], [allowed])).toThrow('安全读取');
      expect(() => readTelegramLocalFiles([url], ['/'])).toThrow('接收目录');
      expect(() => readTelegramLocalFiles([url], ['/tmp/..'])).toThrow('接收目录');
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
  it('rejects missing, non-document and oversized local input without silently substituting another file', () => {
    const dir = realpathSync(mkdtempSync(join(tmpdir(), 'relay-link-test-')));
    try {
      const file = join(dir, 'large.html'); writeFileSync(file, Buffer.alloc(1024 * 1024 + 1));
      expect(() => readTelegramLocalFiles([pathToFileURL(file).href], [dir])).toThrow('大小限制');
      expect(() => readTelegramLocalFiles([pathToFileURL(join(dir, 'missing.html')).href], [dir])).toThrow('不存在');
      const secret = join(dir, 'auth.key'); writeFileSync(secret, 'secret');
      expect(() => readTelegramLocalFiles([pathToFileURL(secret).href], [dir])).toThrow('支持');
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
});


it('takes complete explicit targets, including parentheses, but never an HTTP query parameter', () => {
  const url = 'file:///tmp/diagram(1).html#1';
  expect(telegramFileLinks(url)).toEqual([url]);
  expect(telegramFileLinks(url, [{ type: 'text_link', offset: 0, length: url.length, url: 'https://example.org/' }])).toEqual([]);
  expect(telegramFileLinks('看图', [{ type: 'text_link', offset: 0, length: 2, url }])).toEqual([url]);
  expect(telegramFileLinks('https://example.org/?data=file:///private/page.html')).toEqual([]);
});

it('keeps reads anchored to an open directory when its pathname is replaced', () => {
  const helper = fileURLToPath(new URL('../runtime/telegram-local-file.py', import.meta.url));
  const python = process.env.PULSE_COMPUTE_COST_PYTHON || process.env.HARNESS_CANONICAL_PYTHON
    || join(userInfo().homedir, '.local/share/coevolution/python-env/bin/python');
  const result = execFileSync(python, ['-I', '-B', '-c', `
import os,runpy,tempfile,pathlib,sys
read=runpy.run_path(sys.argv[1])['read_rooted']
with tempfile.TemporaryDirectory() as d:
 p=pathlib.Path(d).resolve(); allowed=p/'allowed'; nested=allowed/'nested'; outside=p/'outside'
 nested.mkdir(parents=True); outside.mkdir()
 (nested/'page.html').write_text('authorized'); (outside/'page.html').write_text('outside-not-authorized')
 original=os.open
 def replaced(path,flags,*args,**kwargs):
  if path=='page.html':
   nested.rename(allowed/'retained'); nested.symlink_to(outside,target_is_directory=True)
  return original(path,flags,*args,**kwargs)
 os.open=replaced
 try: assert read(str(allowed),'nested/page.html',1024)==b'authorized'
 finally: os.open=original
 try: read(str(allowed),'nested/page.html',1024)
 except OSError: pass
 else: raise AssertionError('followed substituted symlink')
 print('anchored')
`, helper], { encoding: 'utf8' });
  expect(result.trim()).toBe('anchored');
});

it('rejects replacement of the configured root itself with an outside symlink', () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), 'relay-root-test-')));
  try {
    const allowed = join(dir, 'allowed'), outside = join(dir, 'outside');
    mkdirSync(allowed); mkdirSync(outside);
    writeFileSync(join(allowed, 'page.html'), 'authorized');
    writeFileSync(join(outside, 'page.html'), 'outside-not-authorized');
    const url = pathToFileURL(join(allowed, 'page.html')).href;
    expect(readTelegramLocalFiles([url], [allowed])[0].bytes.toString()).toBe('authorized');
    renameSync(allowed, join(dir, 'retained')); symlinkSync(outside, allowed);
    expect(() => readTelegramLocalFiles([url], [allowed])).toThrow('安全读取');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
