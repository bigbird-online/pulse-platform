import { existsSync, readFileSync, statSync } from 'node:fs';
import { basename, join, resolve, sep } from 'node:path';
import type { ServerResponse } from 'node:http';
import { fileURLToPath } from 'node:url';

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.woff2': 'font/woff2',
};

/** 与编译后的 `dist/http-server.js` 同级的 `dist/web`（`npm run build:web` 产出）。 */
export function defaultWebRoot(): string {
  return join(fileURLToPath(new URL('.', import.meta.url)), 'web');
}

/** 防 `..` 穿越；返回磁盘绝对路径或 `undefined`。 */
export function resolveSafeWebPath(webRootAbs: string, urlPath: string): string | undefined {
  const pathOnly = urlPath.split('?')[0] ?? '/';
  let rel = pathOnly === '/' || pathOnly === '' ? '' : pathOnly.slice(1);
  if (rel.includes('\0')) return undefined;
  const segments = rel.split('/').filter((s) => s !== '.' && s !== '');
  if (segments.some((s) => s === '..')) return undefined;
  const candidate = join(webRootAbs, ...segments);
  const rootWithSep = resolve(webRootAbs) + sep;
  const resolved = resolve(candidate);
  if (!resolved.startsWith(rootWithSep) && resolved !== resolve(webRootAbs)) {
    return undefined;
  }
  return resolved;
}

function mimeFor(filePath: string): string {
  const i = filePath.lastIndexOf('.');
  const ext = i >= 0 ? filePath.slice(i) : '';
  return MIME[ext] ?? 'application/octet-stream';
}

function pathHasFileExtension(pathOnly: string): boolean {
  const leaf = basename(pathOnly);
  return /\.[a-z0-9]+$/i.test(leaf);
}

/**
 * 提供 Vite build 静态资源：`/` → `index.html`；`/assets/*` 等带后缀路径按文件读盘。
 * 无文件且无后缀的路径返回 `false`（由上层响应 404），避免误把任意路径当 SPA。
 */
export function tryServeWebRoot(
  webRoot: string,
  pathname: string,
  res: ServerResponse,
): boolean {
  if (!webRoot || !existsSync(webRoot)) {
    return false;
  }
  const webRootAbs = resolve(webRoot);
  const pathOnly = pathname.split('?')[0] ?? '/';

  const sendFile = (abs: string): boolean => {
    if (!existsSync(abs)) return false;
    const st = statSync(abs);
    if (!st.isFile()) return false;
    const buf = readFileSync(abs);
    res.writeHead(200, { 'Content-Type': mimeFor(abs) });
    res.end(buf);
    return true;
  };

  if (pathOnly === '/' || pathOnly === '') {
    return sendFile(join(webRootAbs, 'index.html'));
  }

  const safe = resolveSafeWebPath(webRootAbs, pathOnly);
  if (!safe) return false;

  if (sendFile(safe)) return true;
  if (sendFile(join(safe, 'index.html'))) return true;

  if (pathHasFileExtension(pathOnly)) return false;

  return false;
}
