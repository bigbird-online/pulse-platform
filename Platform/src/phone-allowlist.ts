import { readFileSync, existsSync } from 'node:fs';

/**
 * 将常见中国大陆手机号 / 显式 +86 输入规范为 E.164（+86…）。
 */
export function normalizeCnPhone(input: string): string {
  const trimmed = input.trim().replace(/\s+/g, '');
  const digitsOnly = trimmed.replace(/\D/g, '');
  if (trimmed.startsWith('+')) {
    return `+${digitsOnly}`;
  }
  if (digitsOnly.length === 11 && digitsOnly.startsWith('1')) {
    return `+86${digitsOnly}`;
  }
  return `+${digitsOnly}`;
}

/** 解析允许名单文本：一行一号，忽略空行与 `#` 行注释。 */
export function parseAllowlistLines(text: string): Set<string> {
  const out = new Set<string>();
  for (const line of text.split(/\r?\n/)) {
    const hash = line.indexOf('#');
    const content = (hash >= 0 ? line.slice(0, hash) : line).trim();
    const s = content;
    if (!s || s.startsWith('#')) continue;
    out.add(normalizeCnPhone(s));
  }
  return out;
}

export class PhoneAllowlist {
  constructor(private readonly phones: ReadonlySet<string>) {}

  static loadFromFile(path: string): PhoneAllowlist {
    if (!existsSync(path)) {
      return new PhoneAllowlist(new Set());
    }
    const raw = readFileSync(path, 'utf8');
    return new PhoneAllowlist(parseAllowlistLines(raw));
  }

  /**
   * 自环境变量装载：`PLATFORM_PHONE_ALLOWLIST_PATH` 指向文件；
   * 若未设置或文件不存在，返回空名单（一律拒绝）。
   */
  static fromEnv(env: NodeJS.ProcessEnv = process.env): PhoneAllowlist {
    const p = env['PLATFORM_PHONE_ALLOWLIST_PATH']?.trim();
    if (!p) return new PhoneAllowlist(new Set());
    return PhoneAllowlist.loadFromFile(p);
  }

  /** 合并文件名单与 `PLATFORM_PHONE_ALLOWLIST_INLINE`（逗号分隔）。 */
  static fromEnvWithInline(env: NodeJS.ProcessEnv = process.env): PhoneAllowlist {
    const merged = new Set<string>();
    const path = env['PLATFORM_PHONE_ALLOWLIST_PATH']?.trim();
    if (path && existsSync(path)) {
      for (const p of parseAllowlistLines(readFileSync(path, 'utf8'))) merged.add(p);
    }
    const inline = env['PLATFORM_PHONE_ALLOWLIST_INLINE']?.trim();
    if (inline) {
      for (const part of inline.split(',')) {
        const s = part.trim();
        if (s) merged.add(normalizeCnPhone(s));
      }
    }
    return new PhoneAllowlist(merged);
  }

  isAllowed(phone: string): boolean {
    return this.phones.has(normalizeCnPhone(phone));
  }

  size(): number {
    return this.phones.size;
  }
}
