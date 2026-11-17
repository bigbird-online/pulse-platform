/**
 * P1-001：生产支付适配契约。真实微信/支付宝 SDK 在此接口后接入；未配置时在 production 应 fail-closed。
 */
export type PaymentVerificationContext = {
  headers: Record<string, string | string[] | undefined>;
  rawBody: string;
};

export type PaymentVerifiedEvent =
  | { kind: 'paid'; providerOrderId: string; platformOrderId: string; amountMinor?: number }
  | { kind: 'ignored'; reason: string };

export interface PaymentProvider {
  verifyNotification(ctx: PaymentVerificationContext): Promise<PaymentVerifiedEvent>;
}

export class UnconfiguredPaymentProvider implements PaymentProvider {
  async verifyNotification(): Promise<PaymentVerifiedEvent> {
    return { kind: 'ignored', reason: 'payment_provider_unconfigured' };
  }
}

/** 生产环境占位：显式拒绝，避免静默丢单。 */
export class ProductionPaymentNotImplementedError extends Error {
  constructor() {
    super('payment_provider_not_implemented');
    this.name = 'ProductionPaymentNotImplementedError';
  }
}
