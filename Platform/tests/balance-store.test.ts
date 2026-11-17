import { describe, it, expect } from 'vitest';
import { BalanceStore } from '../src/balance-store.js';

describe('BalanceStore', () => {
  it('creates account on first top-up', () => {
    const store = new BalanceStore();
    const account = store.createOrTopUp('member-001', 10000); // ¥100
    expect(account.tenantId).toBe('member-001');
    expect(account.balance).toBe(10000);
    expect(account.totalRecharged).toBe(10000);
    expect(account.totalConsumed).toBe(0);
  });

  it('tops up existing account', () => {
    const store = new BalanceStore();
    store.createOrTopUp('member-001', 10000);
    const account = store.createOrTopUp('member-001', 5000);
    expect(account.balance).toBe(15000);
    expect(account.totalRecharged).toBe(15000);
  });

  it('returns null for unknown tenant', () => {
    const store = new BalanceStore();
    expect(store.getBalance('unknown')).toBeNull();
  });

  it('deducts balance', () => {
    const store = new BalanceStore();
    store.createOrTopUp('member-001', 10000);
    const result = store.deduct('member-001', 300);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.newBalance).toBe(9700);
  });

  it('rejects deduction for insufficient balance', () => {
    const store = new BalanceStore();
    store.createOrTopUp('member-001', 100);
    const result = store.deduct('member-001', 200);
    expect(result.ok).toBe(false);
    if (!result.ok && result.error === 'insufficient_balance') {
      expect(result.balance).toBe(100);
    }
  });

  it('rejects deduction for unknown tenant', () => {
    const store = new BalanceStore();
    const result = store.deduct('unknown', 100);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toBe('account_not_found');
  });

  it('tracks totalConsumed', () => {
    const store = new BalanceStore();
    store.createOrTopUp('member-001', 10000);
    store.deduct('member-001', 300);
    store.deduct('member-001', 500);
    const account = store.getAccount('member-001');
    expect(account?.totalConsumed).toBe(800);
    expect(account?.balance).toBe(9200);
  });

  it('hasBalance returns true when balance > 0', () => {
    const store = new BalanceStore();
    store.createOrTopUp('member-001', 100);
    expect(store.hasBalance('member-001')).toBe(true);
    expect(store.hasBalance('unknown')).toBe(false);
  });

  it('hasBalance returns false when balance = 0', () => {
    const store = new BalanceStore();
    store.createOrTopUp('member-001', 100);
    store.deduct('member-001', 100);
    expect(store.hasBalance('member-001')).toBe(false);
  });

  it('listAccounts returns all accounts', () => {
    const store = new BalanceStore();
    store.createOrTopUp('member-001', 10000);
    store.createOrTopUp('member-002', 20000);
    const list = store.listAccounts();
    expect(list).toHaveLength(2);
  });

  it('throws on non-positive top-up amount', () => {
    const store = new BalanceStore();
    expect(() => store.createOrTopUp('member-001', 0)).toThrow();
    expect(() => store.createOrTopUp('member-001', -100)).toThrow();
  });
});
