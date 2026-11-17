import { useState } from 'react';
import type { Capabilities, ModelGroup, OrderStatus, PresetItem } from '../types';
import { LoadingBlock } from './LoadingBlock';

type NativeData = { order_no: string; qrcode_base64: string };

type Props = {
  caps: Capabilities | null;
  step: number;
  busy: boolean;
  phone: string;
  displayName: string;
  modelGroup: string;
  topUpAmount: number;
  customAmount: string;
  orderId: string;
  orderStatus: OrderStatus;
  subErr: string | null;
  nativeData: NativeData | null;
  nativeErr: string | null;
  recommendedFeishuBotName: string;
  checkOrderPaid: () => Promise<boolean>;
  setPhone: (v: string) => void;
  setDisplayName: (v: string) => void;
  setModelGroup: (v: string) => void;
  setTopUpAmount: (v: number) => void;
  setCustomAmount: (v: string) => void;
  createOrder: () => void;
  mockPay: () => void;
};

export function Step1PlanSelection({
  caps,
  step,
  busy,
  phone,
  displayName,
  modelGroup,
  topUpAmount,
  customAmount,
  orderId,
  orderStatus,
  subErr,
  nativeData,
  nativeErr,
  recommendedFeishuBotName,
  setPhone,
  setDisplayName,
  setModelGroup,
  setTopUpAmount,
  setCustomAmount,
  createOrder,
  mockPay,
  checkOrderPaid,
}: Props) {
  const [touched, setTouched] = useState({ phone: false, displayName: false });

  const phoneValid = /^\d{11}$/.test(phone.trim());
  const displayNameValid = displayName.trim().length > 0;
  const showPhoneError = touched.phone && !phoneValid && phone.length > 0;
  const showDisplayNameError = touched.displayName && !displayNameValid && displayName.length > 0;

  const handleBlur = (field: 'phone' | 'displayName') => {
    setTouched((prev) => ({ ...prev, [field]: true }));
  };

  const handleSubmit = () => {
    setTouched({ phone: true, displayName: true });
    if (!phoneValid || !displayNameValid) return;
    createOrder();
  };

  const modelGroups: ModelGroup[] = caps?.modelGroups ?? [];
  const rawPresets = caps?.topUpPresets ?? [50, 100, 200, 500];
  // Normalize to PresetItem[] (support legacy number[] format)
  const presets: PresetItem[] = (rawPresets as unknown[]).map(p =>
    typeof p === 'object' && p !== null && 'amount' in p ? p as PresetItem : { amount: p as number, credit: p as number, bonus: 0 }
  );
  const amounts = presets.map(p => p.amount);
  const minTopUp = caps?.minTopUpYuan ?? 50;
  const isCustom = !amounts.includes(topUpAmount);
  const topUpValid = topUpAmount >= minTopUp;
  const formValid = phoneValid && displayNameValid && modelGroup && topUpValid;

  return (
    <>
      {/* Basic info */}
      <section className="card">
        <h2>1 · 充值并开通</h2>
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          预付费按量扣减，充多少用多少。建议在飞书开放平台中将自建应用命名为
          <code className="mono"> {recommendedFeishuBotName} </code>。
        </p>
        <label htmlFor="phone">手机号</label>
        <input
          id="phone"
          type="tel"
          placeholder="例如 13800138000"
          value={phone}
          disabled={step > 1}
          onChange={(e) => setPhone(e.target.value)}
          onBlur={() => handleBlur('phone')}
          autoComplete="tel"
          className={showPhoneError ? 'input-error' : ''}
        />
        {showPhoneError ? <div className="field-error">请输入 11 位手机号</div> : null}
        <label htmlFor="displayName">用户名（你的称呼）</label>
        <input
          id="displayName"
          placeholder="例如 聪聪"
          value={displayName}
          disabled={step > 1}
          onChange={(e) => setDisplayName(e.target.value)}
          onBlur={() => handleBlur('displayName')}
          className={showDisplayNameError ? 'input-error' : ''}
        />
      </section>

      {/* Model group + top-up amount selection (before order created) */}
      {!orderId && step === 1 ? (
        <section className="card">
          {/* Model group */}
          {modelGroups.length > 0 ? (
            <>
              <h3 style={{ marginBottom: '12px' }}>选择模型组</h3>
              <div className="step1-plans">
                {modelGroups.map((g) => {
                  const selected = modelGroup === g.id;
                  const isForeign = g.id === 'foreign';
                  return (
                    <div
                      key={g.id}
                      className={`step1-plan-card${selected ? ' step1-plan-selected' : ''}${isForeign ? ' step1-plan-disabled' : ''}`}
                      onClick={() => { if (!isForeign) setModelGroup(g.id); }}
                    >
                      {isForeign && <span className="step1-plan-badge">即将开放</span>}
                      <h4>{g.display_name}</h4>
                      <p className="step1-plan-desc">{g.description}</p>
                      <span className="step1-plan-tier">{g.pricing_tier}</span>
                    </div>
                  );
                })}
              </div>
            </>
          ) : null}

          {/* Top-up amount */}
          <h3 style={{ marginTop: '20px', marginBottom: '12px' }}>充值金额</h3>
          <div className="step1-topup-presets">
            {presets.map((p) => (
              <button
                key={p.amount}
                type="button"
                className={`step1-topup-btn${!isCustom && topUpAmount === p.amount ? ' step1-topup-selected' : ''}`}
                onClick={() => { setTopUpAmount(p.amount); setCustomAmount(''); }}
              >
                <span className="step1-topup-amount">¥{p.amount}</span>
                {p.bonus > 0 && <span className="step1-topup-credit">到账 ¥{p.credit}</span>}
              </button>
            ))}
            <button
              type="button"
              className={`step1-topup-btn step1-topup-custom${isCustom ? ' step1-topup-selected' : ''}`}
              onClick={() => setTopUpAmount(0)}
            >
              自定义
            </button>
          </div>

          {isCustom || topUpAmount === 0 ? (
            <div style={{ marginTop: '12px' }}>
              <input
                type="number"
                placeholder={`最低 ¥${minTopUp}`}
                value={customAmount}
                onChange={(e) => {
                  setCustomAmount(e.target.value);
                  const n = parseInt(e.target.value, 10);
                  if (!isNaN(n) && n > 0) setTopUpAmount(n);
                  else setTopUpAmount(0);
                }}
                min={minTopUp}
                className={customAmount && !topUpValid ? 'input-error' : ''}
              />
              {customAmount && !topUpValid ? (
                <div className="field-error">充值金额最低 ¥{minTopUp}</div>
              ) : null}
            </div>
          ) : null}

          {subErr ? <div className="msg error" role="alert">{subErr}</div> : null}

          <div className="row" style={{ marginTop: '20px' }}>
            <button
              type="button"
              className="btn-primary"
              disabled={busy || !formValid}
              onClick={handleSubmit}
            >
              {busy ? '正在创建订单…' : `充值 ¥${topUpValid ? topUpAmount : '—'} 并开通`}
            </button>
          </div>

          {caps?.billingNotice ? (
            <p className="plan-notice" style={{ marginBottom: '0' }}>
              💡 {caps.billingNotice}
            </p>
          ) : (
            <p className="plan-notice">
              预付费按量扣减，余额可随时充值。点击后将创建充值订单并跳转微信支付。
            </p>
          )}
        </section>
      ) : null}

      {/* Payment pending — show QR code or waiting state */}
      {orderId && step === 1 && orderStatus === 'pending' ? (
        <section className="card">
          <h2>1.5 · 完成支付</h2>
          <div className="payment-amount">¥{topUpAmount}</div>
          <p className="payment-description">预付费充值 · 按量扣减</p>

          {nativeErr ? (
            <div className="msg error" role="alert">{nativeErr}</div>
          ) : null}

          {nativeData?.qrcode_base64 ? (
            <>
              <div className="payment-qrcode">
                <img
                  src={nativeData.qrcode_base64.startsWith('data:') ? nativeData.qrcode_base64 : `data:image/png;base64,${nativeData.qrcode_base64}`}
                  alt="支付二维码"
                  width={240}
                  height={240}
                />
              </div>
              <p className="payment-instructions">请使用微信扫描二维码完成支付</p>
            </>
          ) : !nativeErr ? (
            <LoadingBlock text="正在加载支付二维码…" />
          ) : null}

          <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '12px' }}>
            支付成功后页面将自动进入下一步…
          </p>

          <button
            type="button"
            className="btn-secondary"
            style={{ marginTop: '16px' }}
            disabled={busy}
            onClick={async () => {
              const paid = await checkOrderPaid();
              if (!paid) {
                alert('尚未检测到支付成功，请稍后再试。');
              }
            }}
          >
            我已支付，检查状态
          </button>

          {caps?.mockPay ? (
            <div style={{ marginTop: '24px', paddingTop: '20px', borderTop: '1px solid var(--border-default)' }}>
              <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginBottom: '12px' }}>
                开发环境可直接模拟支付。
              </p>
              <button type="button" className="btn-secondary" disabled={busy} onClick={mockPay}>
                模拟支付成功
              </button>
            </div>
          ) : null}
        </section>
      ) : null}
    </>
  );
}
