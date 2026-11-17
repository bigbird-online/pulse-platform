import { relayChildEnv } from '../relay-secrets.js';
import { execFileSync } from 'node:child_process';
import { userInfo } from 'node:os';
import { dirname, extname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

function linkEntities(text: string, entities?: unknown): Array<{ type: string; url?: string; offset: number; length: number }> {
  return Array.isArray(entities) ? entities.filter(entity => entity
    && Number.isInteger(entity.offset) && Number.isInteger(entity.length)
    && entity.offset >= 0 && entity.length > 0 && entity.offset + entity.length <= text.length) : [];
}

/** Telegram entity offsets are UTF-16 offsets, just like JavaScript slices. */
export function telegramTextWithLinks(text: string, entities?: unknown): string {
  let result = text;
  for (const entity of linkEntities(text, entities).filter(e => e.type === 'text_link' && typeof e.url === 'string')
    .sort((a, b) => b.offset - a.offset)) {
    const end = entity.offset + entity.length;
    result = result.slice(0, end) + ` (${entity.url})` + result.slice(end);
  }
  return result;
}

/** Only the current message's explicit URLs, never expanded prompts or reply text. */
export function telegramFileLinks(text: string, entities?: unknown): string[] {
  const targets = linkEntities(text, entities).flatMap(entity => entity.type === 'text_link' && entity.url
    ? [entity.url] : entity.type === 'url' ? [text.slice(entity.offset, entity.offset + entity.length)] : []);
  // Parentheses are valid filename characters; the entity target is never reparsed
  // from rendered Markdown. A file: string inside an HTTP query is not a file URL.
  for (const match of text.matchAll(/(?:^|\s)(file:\/\/[^\s<>"']+)/giu)) {
    const start = match.index! + match[0].length - match[1].length;
    if (!linkEntities(text, entities).some(entity => entity.offset < start + match[1].length && entity.offset + entity.length > start)) targets.push(match[1]);
  }
  return [...new Set(targets.filter(url => /^file:\/\//i.test(url)))];
}

export interface TelegramLocalFile { url: string; path: string; bytes: Buffer; mediaType?: string }

export function readTelegramLocalFiles(urls: string[], roots: string[] = []): TelegramLocalFile[] {
  if (urls.length > 4) throw new Error('一次最多读取 4 个本地文件链接。');
  // Operator-configured physical roots stay fixed; never authorize the target of
  // a root symlink substituted after configuration. The helper rejects every link.
  const allowed = roots.filter(root => isAbsolute(root) && resolve(root) !== '/').map(root => resolve(root));
  let total = 0;
  return urls.map(url => {
    let path: string;
    try { path = resolve(fileURLToPath(new URL(url))); }
    catch { throw new Error('本地文件链接不存在、无效或不是本机文件，请直接发送该文件。'); }
    const within = (root: string) => {
      const rel = relative(root, path);
      return rel !== '' && rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
    };
    const root = allowed.find(within);
    if (!root) throw new Error('本地文件不在此用户已配置的接收目录内，请直接发送该文件。');
    const mediaType = ({ '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
      '.webp': 'image/webp', '.gif': 'image/gif' } as Record<string, string>)[extname(path).toLowerCase()];
    if (!mediaType && !/\.(html?|txt|md|csv|tsv|json)$/i.test(path)) {
      throw new Error('本地链接支持图片、HTML 和文本文件；其它类型请作为文件发送。');
    }
    const limit = Math.min(mediaType ? 20 * 1024 * 1024 : 1024 * 1024, 20 * 1024 * 1024 - total);
    // The resident toolchain supplies canonical Python. openat(dir_fd) is needed
    // on both macOS and Linux: Node's O_NOFOLLOW protects only the final segment.
    const python = process.env.PULSE_COMPUTE_COST_PYTHON || process.env.HARNESS_CANONICAL_PYTHON
      || join(userInfo().homedir, '.local/share/coevolution/python-env/bin/python');
    let bytes: Buffer;
    try {
      bytes = execFileSync(python, ['-I', '-B', join(dirname(fileURLToPath(import.meta.url)), '../../runtime/telegram-local-file.py')], {
        input: JSON.stringify({ root, relative: relative(root, path), limit }), timeout: 5000,
        maxBuffer: limit + 1024, stdio: ['pipe', 'pipe', 'pipe'], env: relayChildEnv(),
      });
    } catch { throw new Error('本地文件不存在、超过大小限制或无法安全读取，请直接发送该文件。'); }
    total += bytes.length;
    return { url, path, bytes, mediaType };
  });
}
