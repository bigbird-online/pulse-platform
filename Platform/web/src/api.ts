import type { ApiResult } from './types';

export function baseUrlJoin(base: string, path: string): string {
  const b = base.endsWith('/') ? base.slice(0, -1) : base;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

export const WEB_BASE =
  (import.meta as unknown as { env?: { BASE_URL?: string } }).env?.BASE_URL ?? '/';

export async function api<T>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  const res = await fetch(baseUrlJoin(WEB_BASE, path), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  let rawText: string | undefined;
  const data = (await res.json().catch(async () => {
    try {
      rawText = await res.text();
    } catch {
      rawText = undefined;
    }
    return {};
  })) as T;
  if (rawText && rawText.length > 2000) rawText = `${rawText.slice(0, 2000)}...`;
  return { ok: res.ok, status: res.status, data, rawText };
}

/** Format fen to yuan with 2 decimal places */
export function fenToYuan(fen: number): string {
  return (fen / 100).toFixed(2);
}

/** Format timestamp to locale date string */
export function formatTime(ts?: number | null): string {
  if (!ts) return '-';
  return new Date(ts).toLocaleString('zh-CN', { hour12: false });
}
