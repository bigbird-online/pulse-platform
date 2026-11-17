/** Shared types for the Platform wizard UI. */

export type PayScenarioKey = 'member' | 'test';

export type PayScenario = { title: string; amount?: string; description: string };

export type OrderStatus = 'pending' | 'paid' | 'failed' | 'refused' | '';

export type StepIdx = 1 | 2 | 3 | 4;

export type ModelGroup = {
  id: string;
  display_name: string;
  description: string;
  pricing_tier: string;
};

export type PresetItem = {
  /** Recharge amount in yuan (user pays this much) */
  amount: number;
  /** Credit amount in yuan (added to balance) */
  credit: number;
  /** Bonus amount in yuan (credit - amount) */
  bonus: number;
};

export type Capabilities = {
  mockPay: boolean;
  provisionConfigured: boolean;
  /** v0.3.0 — set when PLATFORM_RELAY_HTTP_URL is configured. */
  pairingConfigured?: boolean;
  nativePayment: boolean;
  appPlatformPayment?: boolean;
  paymentInstructions: string;
  paymentRefreshInterval: number;
  paymentScenarios: Record<string, PayScenario>;
  /** v0.7.0 — billing model: 'prepaid' */
  billingModel?: 'prepaid' | 'subscription';
  /** v0.8.0 — top-up presets with credit details (new format), or plain number[] (legacy) */
  topUpPresets?: PresetItem[] | number[];
  /** v0.7.0 — minimum top-up in yuan */
  minTopUpYuan?: number;
  /** v0.8.0 — credit multiplier (e.g. 5 means charge ¥100 → credit ¥500) */
  creditMultiplier?: number;
  /** v0.8.0 — monthly minimum recharge amount in yuan */
  monthlyMinimumYuan?: number;
  /** v0.8.0 — human-readable billing notice */
  billingNotice?: string;
  /** v0.7.0 — available model groups */
  modelGroups?: ModelGroup[];
  /** @deprecated v0.7.0 — use billingModel instead */
  subscriptionPlans?: never[];
};

export type ApiResult<T> = {
  ok: boolean;
  status: number;
  data: T;
  rawText?: string;
};

/** Schema persisted to localStorage for wizard progress resumption. */
export type WizardProgress = {
  _v: 2;
  _ts: number;
  step: StepIdx;
  phone: string;
  displayName: string;
  orderId: string;
  orderStatus: OrderStatus;
  topUpAmount: number;
  modelGroup: string;
  feishuAppId: string;
  tenantId: string;
  agentId: string;
  agentName: string;
};

/** Billing balance response (GET /api/billing/balance) */
export type BillingBalance = {
  tenantId: string;
  balance: number;
  totalRecharged: number;
  totalConsumed: number;
  consumptionCount: number;
  lastConsumptionAt?: number;
  /** v0.8.0 — user phone from most recent paid order (for recharge flow) */
  phone?: string;
  exists: boolean;
};

/** Recharge record (from GET /api/billing/recharges) */
export type RechargeRecord = {
  orderId: string;
  amountYuan: number;
  paidAt?: number;
  status: string;
};

/** Consumption record (from GET /api/billing/consumptions) */
export type ConsumptionRecord = {
  id: string;
  tenantId: string;
  amountFen: number;
  description: string;
  createdAt: number;
  metadata?: { modelId?: string; tokensIn?: number; tokensOut?: number };
};

/** Admin billing summary (GET /api/admin/billing/summary) */
export type AdminBillingSummary = {
  overview: {
    totalBalanceFen: number;
    totalRechargedFen: number;
    totalConsumedFen: number;
    tenantCount: number;
    activeTenantCount: number;
    transactionCount: number;
  };
  recharges: {
    todayYuan: number;
    weekYuan: number;
    monthYuan: number;
    totalYuan: number;
  };
  tenants: Array<{
    tenantId: string;
    balanceFen: number;
    totalRechargedFen: number;
    totalConsumedFen: number;
  }>;
};
