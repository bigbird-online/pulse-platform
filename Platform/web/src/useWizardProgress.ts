import { useCallback, useRef } from 'react';
import type { WizardProgress, StepIdx, OrderStatus } from './types';

const STORAGE_KEY = 'platform_wizard_progress';
const MAX_AGE_MS = 24 * 60 * 60 * 1000; // 24 hours

export function useWizardProgress() {
  const loaded = useRef<WizardProgress | null>(null);

  // Read once on first call
  if (loaded.current === undefined) {
    loaded.current = null;
  }
  if (loaded.current === null) {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as WizardProgress;
        // Accept _v: 2 (current); ignore stale _v: 1
        if (parsed._v === 2 && Date.now() - parsed._ts < MAX_AGE_MS && parsed.step < 4) {
          loaded.current = parsed;
        } else {
          localStorage.removeItem(STORAGE_KEY);
        }
      }
    } catch {
      localStorage.removeItem(STORAGE_KEY);
    }
  }

  const save = useCallback((patch: Partial<WizardProgress>) => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      const existing: Partial<WizardProgress> = raw ? JSON.parse(raw) : {};
      // Never persist secrets
      const safe = { ...patch };
      delete (safe as Record<string, unknown>).feishuAppSecret;
      delete (safe as Record<string, unknown>).pairingCode;
      delete (safe as Record<string, unknown>).nativeData;

      const merged: WizardProgress = {
        _v: 2,
        _ts: Date.now(),
        step: (safe.step ?? existing.step ?? 1) as StepIdx,
        phone: (safe.phone ?? existing.phone ?? '') as string,
        displayName: (safe.displayName ?? existing.displayName ?? '') as string,
        orderId: (safe.orderId ?? existing.orderId ?? '') as string,
        orderStatus: (safe.orderStatus ?? existing.orderStatus ?? '') as OrderStatus,
        topUpAmount: (safe.topUpAmount ?? existing.topUpAmount ?? 100) as number,
        modelGroup: (safe.modelGroup ?? existing.modelGroup ?? 'domestic') as string,
        feishuAppId: (safe.feishuAppId ?? existing.feishuAppId ?? '') as string,
        tenantId: (safe.tenantId ?? existing.tenantId ?? '') as string,
        agentId: (safe.agentId ?? existing.agentId ?? '') as string,
        agentName: (safe.agentName ?? existing.agentName ?? '') as string,
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(merged));
    } catch {
      // localStorage may be unavailable (private browsing, quota)
    }
  }, []);

  const clear = useCallback(() => {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // ignore
    }
  }, []);

  const getStored = useCallback((): WizardProgress | null => {
    return loaded.current;
  }, []);

  return { getStored, save, clear };
}
