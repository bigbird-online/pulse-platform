import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useWizardProgress } from './useWizardProgress';

const STORAGE_KEY = 'platform_wizard_progress';

describe('useWizardProgress', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('returns null when no stored progress', () => {
    const { result } = renderHook(() => useWizardProgress());
    expect(result.current.getStored()).toBeNull();
  });

  it('saves and reads progress', () => {
    const { result } = renderHook(() => useWizardProgress());

    act(() => {
      result.current.save({
        step: 2,
        phone: '13800138000',
        displayName: '聪聪',
        orderId: 'ord_123',
        orderStatus: 'paid',
        topUpAmount: 100,
        modelGroup: 'domestic',
        feishuAppId: 'cli_test',
        tenantId: 'member-0001',
        agentId: 'member-0001-pulse',
        agentName: '聪聪的pulse管理员',
      });
    });

    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(stored.step).toBe(2);
    expect(stored.phone).toBe('13800138000');
    expect(stored._v).toBe(2);
    expect(stored.topUpAmount).toBe(100);
    expect(stored.modelGroup).toBe('domestic');
    expect(stored._ts).toBeTypeOf('number');
  });

  it('never persists feishuAppSecret', () => {
    const { result } = renderHook(() => useWizardProgress());

    act(() => {
      result.current.save({
        step: 2,
        phone: '13800138000',
        displayName: '聪聪',
        orderId: 'ord_123',
        orderStatus: 'paid',
        topUpAmount: 100,
        modelGroup: 'domestic',
        feishuAppId: 'cli_test',
        feishuAppSecret: 'secret123',
        tenantId: 'member-0001',
        agentId: 'member-0001-pulse',
        agentName: '聪聪的pulse管理员',
      });
    });

    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(stored.feishuAppSecret).toBeUndefined();
  });

  it('merges with existing progress on subsequent saves', () => {
    const { result } = renderHook(() => useWizardProgress());

    act(() => {
      result.current.save({ step: 1, phone: '13800138000' });
    });

    act(() => {
      result.current.save({ step: 2, displayName: '聪聪' });
    });

    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(stored.step).toBe(2);
    expect(stored.phone).toBe('13800138000');
    expect(stored.displayName).toBe('聪聪');
  });

  it('clears progress', () => {
    const { result } = renderHook(() => useWizardProgress());

    act(() => {
      result.current.save({ step: 3, phone: '13800138000' });
    });
    expect(localStorage.getItem(STORAGE_KEY)).not.toBeNull();

    act(() => {
      result.current.clear();
    });
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('ignores expired progress (> 24h)', () => {
    const expired = {
      _v: 2,
      _ts: Date.now() - 25 * 60 * 60 * 1000, // 25 hours ago
      step: 2,
      phone: '13800138000',
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(expired));

    const { result } = renderHook(() => useWizardProgress());
    expect(result.current.getStored()).toBeNull();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('ignores step 4 progress (completed)', () => {
    const completed = {
      _v: 2,
      _ts: Date.now(),
      step: 4,
      phone: '13800138000',
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(completed));

    const { result } = renderHook(() => useWizardProgress());
    expect(result.current.getStored()).toBeNull();
  });

  it('ignores wrong version', () => {
    const wrongVersion = {
      _v: 1,
      _ts: Date.now(),
      step: 1,
      phone: '13800138000',
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(wrongVersion));

    const { result } = renderHook(() => useWizardProgress());
    expect(result.current.getStored()).toBeNull();
  });
});
