import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export type OrderStatus = 'pending' | 'paid' | 'failed' | 'refunded';

export interface Order {
  id: string;
  phone: string;
  /** v0.3.0: 用户可读的展示名（用于生成 agent.name = `${displayName}的pulse管理员`）。 */
  displayName?: string;
  status: OrderStatus;
  createdAt: number;
  paidAt?: number;
  provisionedAt?: number;
  /** v0.3.0: provision 完成时分配的 `member-NNNN`，pair 阶段会用到。 */
  tenantId?: string;
  /** v0.5.0: App Platform 订阅 ID（v0.6.0 标记废弃，保留兼容）。 */
  subscriptionId?: string;
  /** v0.5.0: 选中的套餐 ID（v0.6.0 标记废弃，保留兼容）。 */
  planId?: string;
  /** v0.6.0: 充值金额（元）。paid 时转为余额。 */
  topUpAmount?: number;
  /** v0.6.0: 模型组（domestic / foreign）。决定租户使用的模型和定价策略。 */
  modelGroup?: string;
  /** v0.8.0: 订单来源。'recharge' = 充值页面创建（跳过灰度/倍率），缺省 = 注册流程。 */
  source?: 'signup' | 'recharge';
}

export type RestoreOrderInput = {
  id: string;
  phone: string;
  displayName?: string;
  status: OrderStatus;
  createdAt?: number;
  paidAt?: number;
  provisionedAt?: number;
  tenantId?: string;
  subscriptionId?: string;
  planId?: string;
  topUpAmount?: number;
  modelGroup?: string;
  source?: 'signup' | 'recharge';
};

export type MarkPaidResult =
  | { success: true; already: boolean }
  | { success: false; error: 'not_found' }
  | { success: false; error: 'invalid_state'; currentStatus: OrderStatus };

/**
 * Listener fired after a successful `markPaid`. `already === true` indicates
 * the order was already in `paid` state (webhook replay / mock-pay retry).
 *
 * Listeners must be **synchronous and idempotent**; the store calls them in
 * registration order and swallows any thrown errors so a failing listener
 * cannot break webhook idempotency.
 */
export type OnPaidListener = (orderId: string, info: { already: boolean }) => void;

/**
 * Order store with JSON file persistence.
 *
 * Records persist across process restarts when `filePath` is provided.
 */
export class OrderStore {
  private readonly orders = new Map<string, Order>();
  private seq = 0;
  private readonly onPaidListeners: OnPaidListener[] = [];
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
      const data = JSON.parse(raw) as Order[];
      for (const order of data) {
        this.orders.set(order.id, order);
      }
      console.log({ ts: new Date().toISOString(), level: 'info', msg: 'OrderStore loaded', count: this.orders.size });
    } catch {
      // File doesn't exist or is invalid — start empty
    }
  }

  private save(): void {
    if (!this.filePath) return;
    try {
      const data = Array.from(this.orders.values());
      mkdirSync(dirname(this.filePath), { recursive: true });
      writeFileSync(this.filePath, JSON.stringify(data, null, 2), 'utf-8');
    } catch (err) {
      console.error({ ts: new Date().toISOString(), level: 'error', msg: 'OrderStore save failed', error: String(err) });
    }
  }

  createPending(input: { phone: string; displayName?: string; planId?: string; topUpAmount?: number; modelGroup?: string; source?: 'signup' | 'recharge' }): string {
    // WeChat out_trade_no: allowed charset + length constraints.
    //
    // CRITICAL: IDs must be unique across process restarts. A pure sequential
    // counter (ord_000001) will be reused after restart, causing the provider
    // to return "already paid" for an old order, which incorrectly advances
    // the onboarding flow.
    //
    // Format: ord_{base36_epoch}_{hex6}
    // - base36_epoch: Date.now() base36 (0-9a-z)
    // - hex6: 3 random bytes (0-9a-f)
    const epoch = Date.now().toString(36);
    const rand = Math.floor(Math.random() * 0xffffff)
      .toString(16)
      .padStart(6, '0');
    const id = `ord_${epoch}_${rand}`;
    const displayName = input.displayName?.trim();
    this.orders.set(id, {
      id,
      phone: input.phone,
      ...(displayName ? { displayName } : {}),
      status: 'pending',
      createdAt: Date.now(),
      ...(input.planId ? { planId: input.planId } : {}),
      ...(input.topUpAmount ? { topUpAmount: input.topUpAmount } : {}),
      ...(input.modelGroup ? { modelGroup: input.modelGroup } : {}),
      ...(input.source ? { source: input.source } : {}),
    });
    this.save();
    return id;
  }

  /** v0.3.0: 在 Step 2 写盘前把分配好的 tenantId 记到订单上（pair 阶段会用到）。 */
  setTenantId(id: string, tenantId: string): void {
    const o = this.orders.get(id);
    if (o) { o.tenantId = tenantId; this.save(); }
  }

  /** v0.5.0: 在 App Platform 返回 subscriptionId 后记录到订单。 */
  setSubscriptionId(id: string, subscriptionId: string): void {
    const o = this.orders.get(id);
    if (o) { o.subscriptionId = subscriptionId; this.save(); }
  }

  get(id: string): Order | undefined {
    return this.orders.get(id);
  }

  /**
   * Administrative: list all orders (for billing dashboard).
   * Returns a shallow copy of all orders.
   */
  listAll(): { items: Order[] } {
    return { items: Array.from(this.orders.values()).map(o => ({ ...o })) };
  }

  /**
   * Administrative escape hatch: restore an order into the in-memory store.
   * Used when the service was restarted after a real payment and the order was
   * lost (M1: no persistence), so the user would otherwise be forced to pay again.
   *
   * If the restored status is `paid`, this will fire `onPaid` listeners to
   * re-run Stage A materialisation idempotently.
   */
  restore(input: RestoreOrderInput): void {
    const id = input.id.trim();
    const phone = input.phone.trim();
    if (!id || !phone) return;
    const displayName = input.displayName?.trim();
    const now = Date.now();
    const order: Order = {
      id,
      phone,
      ...(displayName ? { displayName } : {}),
      status: input.status,
      createdAt: input.createdAt ?? now,
      ...(typeof input.paidAt === 'number' ? { paidAt: input.paidAt } : {}),
      ...(typeof input.provisionedAt === 'number' ? { provisionedAt: input.provisionedAt } : {}),
      ...(input.tenantId ? { tenantId: input.tenantId } : {}),
      ...(input.subscriptionId ? { subscriptionId: input.subscriptionId } : {}),
      ...(input.planId ? { planId: input.planId } : {}),
      ...(input.topUpAmount ? { topUpAmount: input.topUpAmount } : {}),
      ...(input.modelGroup ? { modelGroup: input.modelGroup } : {}),
      ...(input.source ? { source: input.source } : {}),
    };
    this.orders.set(id, order);
    this.save();
    if (order.status === 'paid') this.fireOnPaid(id, true);
  }

  /** 测试 / 负面路径：将订单标为失败，禁止再走 mock paid。 */
  markFailed(id: string): void {
    const o = this.orders.get(id);
    if (o) { o.status = 'failed'; this.save(); }
  }

  /** 退款终态：自 pending 或 paid 转入（不可再支付确认）。 */
  markRefunded(id: string): void {
    const o = this.orders.get(id);
    if (!o) return;
    if (o.status === 'pending' || o.status === 'paid') { o.status = 'refunded'; this.save(); }
  }

  markProvisioned(id: string): void {
    const o = this.orders.get(id);
    if (o) { o.provisionedAt = Date.now(); this.save(); }
  }

  isProvisioned(id: string): boolean {
    return this.orders.get(id)?.provisionedAt != null;
  }

  markPaid(id: string): MarkPaidResult {
    const o = this.orders.get(id);
    if (!o) return { success: false, error: 'not_found' };
    if (o.status === 'paid') {
      this.fireOnPaid(id, true);
      return { success: true, already: true };
    }
    if (o.status !== 'pending') {
      return { success: false, error: 'invalid_state', currentStatus: o.status };
    }
    o.status = 'paid';
    o.paidAt = Date.now();
    this.save();
    this.fireOnPaid(id, false);
    return { success: true, already: false };
  }

  /**
   * Register a listener fired on **every** successful `markPaid` (including
   * idempotent `already=true` retries). Returns an unsubscribe function.
   *
   * Used by the HTTP server to wire Stage A (FR-P0-014) materialisation of
   * the tenant workspace template the moment an order becomes paid.
   */
  onPaid(cb: OnPaidListener): () => void {
    this.onPaidListeners.push(cb);
    return () => {
      const i = this.onPaidListeners.indexOf(cb);
      if (i >= 0) this.onPaidListeners.splice(i, 1);
    };
  }

  private fireOnPaid(orderId: string, already: boolean): void {
    for (const cb of this.onPaidListeners) {
      try {
        cb(orderId, { already });
      } catch (e) {
        // A listener must never break markPaid; log and continue.
        const msg = e instanceof Error ? e.message : String(e);
        console.error(
          JSON.stringify({
            ts: new Date().toISOString(),
            level: 'error',
            msg: 'OrderStore.onPaid listener threw',
            orderId,
            error: msg,
          }),
        );
      }
    }
  }
}
