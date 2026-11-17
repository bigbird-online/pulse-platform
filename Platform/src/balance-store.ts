/**
 * Tenant balance account store with JSON file persistence.
 *
 * Each tenant has one CNY balance account. Balance stored in fen (分) to avoid
 * floating-point issues. Top-up adds to balance; deduction subtracts.
 *
 * Data is persisted to a JSON file on every mutation. On startup, data is
 * loaded from the file if it exists.
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export interface BalanceAccount {
  tenantId: string;
  /** Current balance in CNY fen (分). 1 yuan = 100 fen. */
  balance: number;
  /** Cumulative total recharged in fen. */
  totalRecharged: number;
  /** Cumulative total consumed in fen. */
  totalConsumed: number;
  createdAt: number;  // epoch ms
  updatedAt: number;  // epoch ms
}

export type DeductResult =
  | { ok: true; newBalance: number }
  | { ok: false; error: 'insufficient_balance'; balance: number; shadow?: boolean }
  | { ok: false; error: 'account_not_found' };

export class BalanceStore {
  private readonly accounts = new Map<string, BalanceAccount>();
  private readonly filePath?: string;

  constructor(options?: { filePath?: string }) {
    this.filePath = options?.filePath;
    if (this.filePath) {
      this.load();
    }
  }

  private load(): void {
    if (!this.filePath) return;
    try {
      const raw = readFileSync(this.filePath, 'utf-8');
      const data = JSON.parse(raw) as BalanceAccount[];
      for (const account of data) {
        this.accounts.set(account.tenantId, account);
      }
      console.log({ ts: new Date().toISOString(), level: 'info', msg: 'BalanceStore loaded', count: this.accounts.size });
    } catch {
      // File doesn't exist or is invalid — start empty
    }
  }

  private save(): void {
    if (!this.filePath) return;
    try {
      const data = Array.from(this.accounts.values());
      mkdirSync(dirname(this.filePath), { recursive: true });
      writeFileSync(this.filePath, JSON.stringify(data, null, 2), 'utf-8');
    } catch (err) {
      console.error({ ts: new Date().toISOString(), level: 'error', msg: 'BalanceStore save failed', error: String(err) });
    }
  }

  /**
   * Create or top-up a tenant's balance account. Called on order `paid`.
   * @param amountFen Actual amount paid by user (for totalRecharged tracking).
   * @param creditFen Credit amount added to balance (amountFen × multiplier). Defaults to amountFen.
   */
  createOrTopUp(tenantId: string, amountFen: number, creditFen?: number): BalanceAccount {
    if (amountFen <= 0) throw new Error('amountFen must be positive');
    const credit = creditFen ?? amountFen;
    const now = Date.now();
    const existing = this.accounts.get(tenantId);
    if (existing) {
      existing.balance += credit;
      existing.totalRecharged += amountFen;
      existing.updatedAt = now;
      this.save();
      return { ...existing };
    }
    const account: BalanceAccount = {
      tenantId,
      balance: credit,
      totalRecharged: amountFen,
      totalConsumed: 0,
      createdAt: now,
      updatedAt: now,
    };
    this.accounts.set(tenantId, account);
    this.save();
    return { ...account };
  }

  /** Get balance in fen. Returns null if account not found. */
  getBalance(tenantId: string): number | null {
    const account = this.accounts.get(tenantId);
    return account ? account.balance : null;
  }

  /** Get full account info. Returns undefined if not found. */
  getAccount(tenantId: string): BalanceAccount | undefined {
    const account = this.accounts.get(tenantId);
    return account ? { ...account } : undefined;
  }

  /** Deduct balance. For tenants without an account (e.g. admin), creates a shadow account to track consumption without requiring top-up. */
  deduct(tenantId: string, amountFen: number): DeductResult {
    if (amountFen <= 0) return { ok: true, newBalance: this.accounts.get(tenantId)?.balance ?? 0 };
    let account = this.accounts.get(tenantId);
    if (!account) {
      // Auto-create shadow account for admin/non-billed users — tracks consumption without balance deduction
      const now = Date.now();
      account = {
        tenantId,
        balance: 0,
        totalRecharged: 0,
        totalConsumed: 0,
        createdAt: now,
        updatedAt: now,
      };
      this.accounts.set(tenantId, account);
    }
    if (account.balance < amountFen) {
      // Insufficient balance: still record consumption for tracking, but don't go negative
      account.totalConsumed += amountFen;
      account.updatedAt = Date.now();
      this.save();
      // Shadow accounts (never recharged) — mark so caller knows this is expected
      const isShadow = account.totalRecharged === 0;
      return { ok: false, error: 'insufficient_balance', balance: account.balance, ...(isShadow ? { shadow: true } : {}) };
    }
    account.balance -= amountFen;
    account.totalConsumed += amountFen;
    account.updatedAt = Date.now();
    this.save();
    return { ok: true, newBalance: account.balance };
  }

  /** Check if tenant has sufficient balance (> 0). */
  hasBalance(tenantId: string): boolean {
    const account = this.accounts.get(tenantId);
    return !!account && account.balance > 0;
  }

  /** List all accounts (for admin/status). */
  listAccounts(): BalanceAccount[] {
    return Array.from(this.accounts.values()).map(a => ({ ...a }));
  }
}
