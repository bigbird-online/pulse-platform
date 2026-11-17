/**
 * Tenant consumption record store with JSON file persistence.
 *
 * Records every balance deduction event for billing transparency.
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export interface ConsumptionRecord {
  /** Unique ID (cons_${timestamp}_${random}) */
  id: string;
  tenantId: string;
  /** Amount deducted in CNY fen (分). Positive number. */
  amountFen: number;
  /** Human-readable description (e.g., "模型调用: claude-sonnet-4-7") */
  description: string;
  /** Unix epoch ms */
  createdAt: number;
  /** Optional metadata for analytics */
  metadata?: {
    modelId?: string;
    tokensIn?: number;
    tokensOut?: number;
    sessionId?: string;
  };
}

export interface ListConsumptionsOptions {
  tenantId: string;
  limit?: number;
  offset?: number;
  startTime?: number;
  endTime?: number;
}

export interface ListConsumptionsResult {
  items: ConsumptionRecord[];
  total: number;
  limit: number;
  offset: number;
}

export interface ConsumptionSummary {
  tenantId: string;
  totalConsumedFen: number;
  count: number;
  /** Average consumption per transaction (fen) */
  avgAmountFen: number;
  /** Most recent consumption timestamp */
  lastConsumptionAt?: number;
}

export class ConsumptionStore {
  private readonly records = new Map<string, ConsumptionRecord>();
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
      const data = JSON.parse(raw) as ConsumptionRecord[];
      for (const record of data) {
        const key = this.makeKey(record.tenantId, record.id);
        this.records.set(key, record);
      }
      console.log({ ts: new Date().toISOString(), level: 'info', msg: 'ConsumptionStore loaded', count: this.records.size });
    } catch {
      // File doesn't exist or is invalid — start empty
    }
  }

  private save(): void {
    if (!this.filePath) return;
    try {
      const data = Array.from(this.records.values());
      mkdirSync(dirname(this.filePath), { recursive: true });
      writeFileSync(this.filePath, JSON.stringify(data, null, 2), 'utf-8');
    } catch (err) {
      console.error({ ts: new Date().toISOString(), level: 'error', msg: 'ConsumptionStore save failed', error: String(err) });
    }
  }

  private makeKey(tenantId: string, recordId: string): string {
    return `${tenantId}:${recordId}`;
  }

  private generateId(): string {
    const ts = Date.now().toString(36);
    const rand = Math.floor(Math.random() * 0xffff).toString(16).padStart(4, '0');
    return `cons_${ts}_${rand}`;
  }

  /**
   * Record a consumption event.
   * Called by /internal/balance/deduct when balance is successfully deducted.
   */
  record(input: Omit<ConsumptionRecord, 'id' | 'createdAt'>): ConsumptionRecord {
    if (input.amountFen <= 0) {
      throw new Error('amountFen must be positive');
    }
    const record: ConsumptionRecord = {
      id: this.generateId(),
      tenantId: input.tenantId,
      amountFen: input.amountFen,
      description: input.description || '消费',
      createdAt: Date.now(),
      ...(input.metadata ? { metadata: input.metadata } : {}),
    };
    const key = this.makeKey(record.tenantId, record.id);
    this.records.set(key, record);
    this.save();
    return { ...record };
  }

  /**
   * List consumption records for a tenant.
   * Results ordered by createdAt descending (newest first).
   */
  list(options: ListConsumptionsOptions): ListConsumptionsResult {
    const { tenantId, limit = 20, offset = 0, startTime, endTime } = options;
    const limitClamped = Math.min(100, Math.max(1, limit));
    const offsetClamped = Math.max(0, offset);

    // Collect all records for this tenant
    const tenantRecords: ConsumptionRecord[] = [];
    for (const record of this.records.values()) {
      if (record.tenantId !== tenantId) continue;
      if (startTime && record.createdAt < startTime) continue;
      if (endTime && record.createdAt > endTime) continue;
      tenantRecords.push(record);
    }

    // Sort by createdAt descending
    tenantRecords.sort((a, b) => b.createdAt - a.createdAt);

    const total = tenantRecords.length;
    const items = tenantRecords.slice(offsetClamped, offsetClamped + limitClamped).map(r => ({ ...r }));

    return { items, total, limit: limitClamped, offset: offsetClamped };
  }

  /** Get summary statistics for a tenant. */
  getSummary(tenantId: string): ConsumptionSummary {
    let totalConsumedFen = 0;
    let count = 0;
    let lastConsumptionAt: number | undefined;

    for (const record of this.records.values()) {
      if (record.tenantId !== tenantId) continue;
      totalConsumedFen += record.amountFen;
      count++;
      if (!lastConsumptionAt || record.createdAt > lastConsumptionAt) {
        lastConsumptionAt = record.createdAt;
      }
    }

    return {
      tenantId,
      totalConsumedFen,
      count,
      avgAmountFen: count > 0 ? Math.round(totalConsumedFen / count) : 0,
      lastConsumptionAt,
    };
  }

  /** Get total consumption across all tenants (for admin dashboard). */
  getGlobalSummary(): { totalConsumedFen: number; totalTransactions: number; tenantCount: number } {
    let totalConsumedFen = 0;
    const tenantIds = new Set<string>();

    for (const record of this.records.values()) {
      totalConsumedFen += record.amountFen;
      tenantIds.add(record.tenantId);
    }

    return {
      totalConsumedFen,
      totalTransactions: this.records.size,
      tenantCount: tenantIds.size,
    };
  }

  /** Admin: list all records (for dashboard). */
  listAll(limit = 100, offset = 0): { items: ConsumptionRecord[]; total: number } {
    const limitClamped = Math.min(200, Math.max(1, limit));
    const offsetClamped = Math.max(0, offset);

    const allRecords = Array.from(this.records.values())
      .sort((a, b) => b.createdAt - a.createdAt);

    return {
      items: allRecords.slice(offsetClamped, offsetClamped + limitClamped).map(r => ({ ...r })),
      total: allRecords.length,
    };
  }
}
