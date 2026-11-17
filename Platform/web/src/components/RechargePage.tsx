import { useCallback, useEffect, useRef, useState } from 'react';
import { api, baseUrlJoin, fenToYuan, formatTime, WEB_BASE } from '../api';
import type { BillingBalance, Capabilities, OrderStatus, PresetItem } from '../types';
import { LoadingBlock } from './LoadingBlock';

type Props = {
  onNavigate: (p: 'landing' | 'signup' | 'billing' | 'admin-billing' | 'recharge') => void;
};

export function RechargePage({ onNavigate }: Props) {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [balance, setBalance] = useState<BillingBalance | null>(null);
  const [tenantId, setTenantId] = useState('');
  const [phone, setPhone] = useState('');

  // Form state
  const [topUpAmount, setTopUpAmount] = useState(100);
  const [customAmount, setCustomAmount] = useState('');

  // Order/payment state
  const [orderId, setOrderId] = useState('');
  const [orderStatus, setOrderStatus] = useState<OrderStatus>('');
  const [nativeData, setNativeData] = useState<{ order_no: string; qrcode_base64: string } | null>(null);
  const [nativeErr, setNativeErr] = useState<string | null>(null);
  const [nativeLoading, setNativeLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [paid, setPaid] = useState(false);
  const [tokenAuth, setTokenAuth] = useState<'loading' | 'ok' | 'failed' | null>(null);
  const orderStatusTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Token auth
  useEffect(() => {
    const hash = window.location.hash;
    const hashQuery = hash.includes('?') ? hash.split('?')[1] : '';
    const searchParams = new URLSearchParams(hashQuery);
    const token = searchParams.get('token');
    if (!token) { setTokenAuth('failed'); return; }
    setTokenAuth('loading');
    void (async () => {
      try {
        const res = await api<{ tenantId: string }>(`/api/auth/verify-token?token=${encodeURIComponent(token)}`);
        if (res.ok && res.data.tenantId) {
          setTenantId(res.data.tenantId);
          setTokenAuth('ok');
        } else {
          setTokenAuth('failed');
        }
      } catch {
        setTokenAuth('failed');
      }
    })();
  }, []);

  // Load caps
  useEffect(() => {
    void (async () => {
      const r = await api<Capabilities>('/internal/capabilities');
      if (r.ok) {
        // Filter out test-only presets
        const rawPresets = r.data.topUpPresets ?? [];
        const isObj = rawPresets.length > 0 && typeof rawPresets[0] === 'object' && rawPresets[0] !== null && 'amount' in (rawPresets[0] as object);
        if (isObj) {
          (r.data.topUpPresets as unknown as PresetItem[]) = (rawPresets as unknown as PresetItem[]).filter(p => p.amount >= 50);
        }
        setCaps(r.data);
      }
    })();
  }, []);

  // Load balance when tenantId is ready
  useEffect(() => {
    if (!tenantId) return;
    void (async () => {
      const r = await api<BillingBalance>(`/api/billing/balance?tenantId=${encodeURIComponent(tenantId)}`);
      if (r.ok) {
        setBalance(r.data);
        if (r.data.phone) setPhone(r.data.phone);
      }
    })();
  }, [tenantId]);

  // Normalize presets
  const rawPresets = caps?.topUpPresets ?? [50, 100, 200, 500];
  const presets: PresetItem[] = (rawPresets as unknown[]).map(p =>
    typeof p === 'object' && p !== null && 'amount' in p
      ? p as PresetItem
      : { amount: p as number, credit: p as number, bonus: 0 }
  );
  const amounts = presets.map(p => p.amount);
  const minTopUp = caps?.minTopUpYuan ?? 50;
  const isCustom = !amounts.includes(topUpAmount);
  const topUpValid = topUpAmount >= minTopUp;

  // Create recharge order
  const createRechargeOrder = useCallback(async () => {
    if (!tenantId || !topUpValid) return;
    setBusy(true);
    setNativeErr(null);
    try {
      const r = await api<{ orderId?: string; error?: string }>('/api/billing/recharge', {
        method: 'POST',
        body: JSON.stringify({ tenantId, topUpAmount }),
      });
      if (!r.ok || !r.data.orderId) {
        setNativeErr(r.data.error || '创建订单失败');
        return;
      }
      setOrderId(r.data.orderId);
      setOrderStatus('pending');
    } catch {
      setNativeErr('网络错误');
    } finally {
      setBusy(false);
    }
  }, [tenantId, topUpAmount, topUpValid]);

  // Load native QR after order is created
  const loadNativeQr = useCallback(async () => {
    if (!orderId || !phone || !caps?.nativePayment) return;
    setNativeLoading(true);
    setNativeErr(null);
    try {
      const qs = new URLSearchParams({ type: 'member', phone, internalOrderId: orderId });
      const res = await fetch(baseUrlJoin(WEB_BASE, `/api/payment/create-order?${qs}`));
      const data = (await res.json()) as { success: boolean; error?: string; qrcode_base64?: string; order_no?: string };
      if (!data.success) {
        const errRaw = data.error ?? '';
        const paidLike = errRaw === 'order_already_paid' || errRaw === 'provider_order_already_paid' || /该订单已支付/.test(errRaw);
        if (paidLike) {
          const r = await api<{ status: string }>(`/internal/orders/${orderId}`);
          if (r.ok && r.data.status === 'paid') { setPaid(true); return; }
        }
        setNativeErr(errRaw || '创建支付单失败');
        return;
      }
      setNativeData({ order_no: data.order_no!, qrcode_base64: data.qrcode_base64! });
    } catch {
      setNativeErr('加载二维码失败');
    } finally {
      setNativeLoading(false);
    }
  }, [orderId, phone, caps?.nativePayment]);

  useEffect(() => {
    if (orderId && orderStatus === 'pending') void loadNativeQr();
  }, [orderId, orderStatus, loadNativeQr]);

  // Poll order status
  useEffect(() => {
    if (!orderId || orderStatus !== 'pending') return;
    const check = async () => {
      const r = await api<{ status: OrderStatus }>(`/internal/orders/${orderId}`);
      if (r.ok && r.data.status === 'paid') {
        setOrderStatus('paid');
        setPaid(true);
      }
    };
    void check();
    orderStatusTimer.current = setInterval(() => { void check(); }, 3000);
    return () => { if (orderStatusTimer.current) clearInterval(orderStatusTimer.current); };
  }, [orderId, orderStatus]);

  // Build return URL with token
  const getReturnHash = () => {
    const hash = window.location.hash;
    const hashQuery = hash.includes('?') ? hash.split('?')[1] : '';
    const token = new URLSearchParams(hashQuery).get('token');
    return token ? `#billing?token=${encodeURIComponent(token)}` : '#billing';
  };

  if (tokenAuth === 'loading') {
    return (
      <main className="main"><div className="main-inner">
        <section className="card" style={{ textAlign: 'center' }}><p style={{ color: 'var(--text-muted)' }}>验证链接中...</p></section>
      </div></main>
    );
  }

  if (tokenAuth === 'failed') {
    return (
      <main className="main"><div className="main-inner">
        <section className="card" style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '48px', marginBottom: '16px' }}>🔐</div>
          <h2 style={{ marginBottom: '8px' }}>需要验证身份</h2>
          <p style={{ color: 'var(--text-muted)', fontSize: '14px' }}>
            请通过飞书发送 <code style={{ padding: '2px 8px', borderRadius: '6px', background: 'rgba(255,255,255,0.06)', fontSize: '13px' }}>/balance</code> 获取账户中心链接
          </p>
        </section>
      </div></main>
    );
  }

  return (
    <>
      <section className="hero" style={{ minHeight: '16vh', padding: '80px 24px 24px' }}>
        <div className="hero-inner">
          <p className="hero-eyebrow">账户中心</p>
          <h1 className="hero-title" style={{ fontSize: 'clamp(24px, 3.5vw, 36px)' }}>充值</h1>
        </div>
      </section>

      <main className="main">
        <div className="main-inner">
          {/* Current balance */}
          {balance && (
            <div style={{ textAlign: 'center', marginBottom: '20px', color: 'var(--text-secondary)', fontSize: '14px' }}>
              当前余额 <span style={{ fontWeight: 700, fontSize: '18px', color: 'var(--text-primary)' }}>¥{fenToYuan(balance.balance)}</span>
            </div>
          )}

          {/* Payment success */}
          {paid ? (
            <section className="card" style={{ textAlign: 'center' }}>
              <div style={{ fontSize: '48px', marginBottom: '16px' }}>✅</div>
              <h2 style={{ marginBottom: '8px' }}>充值成功</h2>
              <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>
                已充值 ¥{topUpAmount}
              </p>
              <button type="button" className="btn-primary" onClick={() => {
                window.location.hash = getReturnHash();
                window.location.reload();
              }}>
                返回账户中心
              </button>
            </section>
          ) : !orderId ? (
            /* Select amount */
            <section className="card">
              <h3 style={{ marginBottom: '16px' }}>选择充值金额</h3>
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
                      const n = parseFloat(e.target.value);
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

              {nativeErr && <div className="msg error" role="alert" style={{ marginTop: '12px' }}>{nativeErr}</div>}

              <div style={{ marginTop: '20px' }}>
                <button
                  type="button"
                  className="btn-primary"
                  disabled={busy || !topUpValid}
                  onClick={createRechargeOrder}
                >
                  {busy ? '正在创建订单…' : `立即支付 ¥${topUpValid ? topUpAmount : '—'}`}
                </button>
              </div>

              {caps?.billingNotice && (
                <p className="plan-notice">💡 {caps.billingNotice}</p>
              )}
            </section>
          ) : (
            /* Payment pending — QR code */
            <section className="card">
              <h2>完成支付</h2>
              <div className="payment-amount">¥{topUpAmount}</div>
              <p className="payment-description">预付费充值 · 按量扣减</p>

              {nativeErr ? <div className="msg error" role="alert">{nativeErr}</div> : null}

              {nativeLoading ? (
                <LoadingBlock text="正在加载支付二维码…" />
              ) : nativeData?.qrcode_base64 ? (
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
              ) : null}

              <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '12px' }}>
                支付成功后页面将自动跳转…
              </p>
            </section>
          )}

          <div style={{ textAlign: 'center', marginTop: '24px' }}>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => { window.location.hash = getReturnHash(); window.location.reload(); }}
            >
              ← 返回账户中心
            </button>
          </div>
        </div>
      </main>
    </>
  );
}
