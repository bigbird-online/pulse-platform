import { useCallback, useEffect, useState } from 'react';
import { api, fenToYuan, formatTime } from '../api';
import type { BillingBalance, ConsumptionRecord, RechargeRecord } from '../types';
import { Paginator } from './Paginator';

type Props = {
  onNavigate: (p: 'landing' | 'signup' | 'billing' | 'admin-billing' | 'recharge') => void;
};

type Tab = 'recharges' | 'consumptions';

const PAGE_SIZE = 10;

export function BillingPage({ onNavigate }: Props) {
  const [tenantId, setTenantId] = useState('');
  const [balance, setBalance] = useState<BillingBalance | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tokenAuth, setTokenAuth] = useState<'loading' | 'ok' | 'failed' | null>(null);

  // Tab state
  const [activeTab, setActiveTab] = useState<Tab>('recharges');
  const [recharges, setRecharges] = useState<RechargeRecord[]>([]);
  const [rechargesTotal, setRechargesTotal] = useState(0);
  const [rechargesPage, setRechargesPage] = useState(1);
  const [consumptions, setConsumptions] = useState<ConsumptionRecord[]>([]);
  const [consumptionsTotal, setConsumptionsTotal] = useState(0);
  const [consumptionsPage, setConsumptionsPage] = useState(1);

  const fetchBalance = useCallback(async (tid?: string) => {
    const id = (tid ?? tenantId).trim();
    if (!id) return;
    try {
      const r = await api<BillingBalance>(`/api/billing/balance?tenantId=${encodeURIComponent(id)}`);
      if (r.ok) setBalance(r.data);
    } catch { /* ignore */ }
  }, [tenantId]);

  const fetchRecharges = useCallback(async (tid: string, page: number) => {
    const offset = (page - 1) * PAGE_SIZE;
    try {
      const r = await api<{ items: RechargeRecord[]; total: number }>(
        `/api/billing/recharges?tenantId=${encodeURIComponent(tid)}&limit=${PAGE_SIZE}&offset=${offset}`
      );
      if (r.ok) {
        setRecharges(r.data.items ?? []);
        setRechargesTotal(r.data.total ?? 0);
      }
    } catch { /* ignore */ }
  }, []);

  const fetchConsumptions = useCallback(async (tid: string, page: number) => {
    const offset = (page - 1) * PAGE_SIZE;
    try {
      const r = await api<{ items: ConsumptionRecord[]; total: number }>(
        `/api/billing/consumptions?tenantId=${encodeURIComponent(tid)}&limit=${PAGE_SIZE}&offset=${offset}`
      );
      if (r.ok) {
        setConsumptions(r.data.items ?? []);
        setConsumptionsTotal(r.data.total ?? 0);
      }
    } catch { /* ignore */ }
  }, []);

  // Token auth on mount
  useEffect(() => {
    const hash = window.location.hash;
    const hashQuery = hash.includes('?') ? hash.split('?')[1] : '';
    const searchParams = new URLSearchParams(hashQuery);
    const token = searchParams.get('token');

    if (token) {
      setTokenAuth('loading');
      void (async () => {
        try {
          const res = await api<{ tenantId: string }>(
            `/api/auth/verify-token?token=${encodeURIComponent(token)}`
          );
          if (res.ok && res.data.tenantId) {
            setTenantId(res.data.tenantId);
            setTokenAuth('ok');
          } else {
            setTokenAuth('failed');
            setError('链接已过期或无效，请重新获取。');
          }
        } catch {
          setTokenAuth('failed');
          setError('验证失败，请重试。');
        }
      })();
      return;
    }
    setTokenAuth('failed');
  }, []);

  // Fetch data when tenantId is ready
  useEffect(() => {
    if (tokenAuth !== 'ok' || !tenantId.trim()) return;
    setLoading(true);
    void (async () => {
      await fetchBalance(tenantId);
      await fetchRecharges(tenantId, rechargesPage);
      await fetchConsumptions(tenantId, consumptionsPage);
      setLoading(false);
    })();
  }, [tokenAuth, tenantId, fetchBalance, fetchRecharges, fetchConsumptions, rechargesPage, consumptionsPage]);

  // Refetch when page changes
  useEffect(() => {
    if (!tenantId) return;
    if (activeTab === 'recharges') void fetchRecharges(tenantId, rechargesPage);
  }, [activeTab, rechargesPage, tenantId, fetchRecharges]);

  useEffect(() => {
    if (!tenantId) return;
    if (activeTab === 'consumptions') void fetchConsumptions(tenantId, consumptionsPage);
  }, [activeTab, consumptionsPage, tenantId, fetchConsumptions]);

  // Build recharge link with token
  const getRechargeHash = () => {
    const hash = window.location.hash;
    const hashQuery = hash.includes('?') ? hash.split('?')[1] : '';
    const token = new URLSearchParams(hashQuery).get('token');
    return token ? `#recharge?token=${encodeURIComponent(token)}` : '#recharge';
  };

  if (tokenAuth === 'loading') {
    return (
      <>
        <section className="hero" style={{ minHeight: '24vh', padding: '100px 24px 32px' }}>
          <div className="hero-inner">
            <p className="hero-eyebrow">账户中心</p>
            <h1 className="hero-title" style={{ fontSize: 'clamp(28px, 4vw, 40px)' }}>余额与账单</h1>
          </div>
        </section>
        <main className="main"><div className="main-inner">
          <section className="card" style={{ textAlign: 'center' }}><p style={{ color: 'var(--text-muted)' }}>验证链接中...</p></section>
        </div></main>
      </>
    );
  }

  if (tokenAuth === 'failed') {
    return (
      <>
        <section className="hero" style={{ minHeight: '24vh', padding: '100px 24px 32px' }}>
          <div className="hero-inner">
            <p className="hero-eyebrow">账户中心</p>
            <h1 className="hero-title" style={{ fontSize: 'clamp(28px, 4vw, 40px)' }}>余额与账单</h1>
          </div>
        </section>
        <main className="main"><div className="main-inner">
          <section className="card" style={{ textAlign: 'center' }}>
            <div style={{ fontSize: '48px', marginBottom: '16px' }}>🔐</div>
            <h2 style={{ marginBottom: '8px' }}>需要验证身份</h2>
            <p style={{ color: 'var(--text-muted)', fontSize: '14px' }}>
              请通过飞书发送 <code style={{ padding: '2px 8px', borderRadius: '6px', background: 'rgba(255,255,255,0.06)', fontSize: '13px' }}>/balance</code> 获取账户中心链接
            </p>
          </section>
        </div></main>
      </>
    );
  }

  return (
    <>
      <section className="hero" style={{ minHeight: '24vh', padding: '100px 24px 32px' }}>
        <div className="hero-inner">
          <p className="hero-eyebrow">账户中心</p>
          <h1 className="hero-title" style={{ fontSize: 'clamp(28px, 4vw, 40px)' }}>余额与账单</h1>
          <p className="hero-subtitle">查看充值记录、消费明细和账户余额</p>
        </div>
      </section>

      <main className="main">
        <div className="main-inner">
          {error && (
            <div style={{ padding: '12px 16px', borderRadius: '12px', background: 'rgba(239,68,68,0.1)', color: '#f87171', fontSize: '14px', marginBottom: '16px' }}>
              {error}
            </div>
          )}

          {loading && !balance && (
            <section className="card" style={{ textAlign: 'center' }}><p style={{ color: 'var(--text-muted)' }}>加载中...</p></section>
          )}

          {balance && (
            <>
              {/* Balance overview */}
              <section className="card">
                <h2>账户概览</h2>
                <div style={{ textAlign: 'center', padding: '16px 0' }}>
                  <div style={{ fontSize: '14px', color: 'var(--text-muted)', marginBottom: '4px' }}>当前余额</div>
                  <div style={{
                    fontSize: '48px',
                    fontWeight: 900,
                    background: 'linear-gradient(135deg, #f59e0b, #6d28d9)',
                    WebkitBackgroundClip: 'text',
                    WebkitTextFillColor: 'transparent',
                    letterSpacing: '-0.02em',
                  }}>
                    ¥{fenToYuan(balance.balance)}
                  </div>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '16px', marginTop: '16px' }}>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: '13px', color: 'var(--text-muted)' }}>累计充值</div>
                    <div style={{ fontSize: '18px', fontWeight: 700, color: '#22c55e' }}>¥{fenToYuan(balance.totalRecharged)}</div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: '13px', color: 'var(--text-muted)' }}>累计消费</div>
                    <div style={{ fontSize: '18px', fontWeight: 700, color: '#f87171' }}>¥{fenToYuan(balance.totalConsumed)}</div>
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: '13px', color: 'var(--text-muted)' }}>消费笔数</div>
                    <div style={{ fontSize: '18px', fontWeight: 700 }}>{balance.consumptionCount}</div>
                  </div>
                </div>
                <div style={{ marginTop: '20px', textAlign: 'center' }}>
                  <button
                    type="button"
                    className="btn-primary"
                    onClick={() => { window.location.hash = getRechargeHash(); }}
                  >
                    💳 立即充值
                  </button>
                </div>
              </section>

              {/* Tab: Recharges / Consumptions */}
              <section className="card">
                <div className="billing-tabs">
                  <button
                    type="button"
                    className={`billing-tab${activeTab === 'recharges' ? ' billing-tab-active' : ''}`}
                    onClick={() => setActiveTab('recharges')}
                  >
                    充值记录 {rechargesTotal > 0 ? `(${rechargesTotal})` : ''}
                  </button>
                  <button
                    type="button"
                    className={`billing-tab${activeTab === 'consumptions' ? ' billing-tab-active' : ''}`}
                    onClick={() => setActiveTab('consumptions')}
                  >
                    消费明细 {consumptionsTotal > 0 ? `(${consumptionsTotal})` : ''}
                  </button>
                </div>

                {activeTab === 'recharges' && (
                  <>
                    {recharges.length === 0 ? (
                      <p className="billing-empty">暂无充值记录</p>
                    ) : (
                      <div className="billing-list">
                        {recharges.map(r => (
                          <div key={r.orderId} className="billing-item">
                            <div>
                              <div className="billing-item-title">+¥{r.amountYuan}</div>
                              <div className="billing-item-time">{formatTime(r.paidAt)}</div>
                            </div>
                            <span className="billing-badge billing-badge-green">已到账</span>
                          </div>
                        ))}
                      </div>
                    )}
                    <Paginator
                      total={rechargesTotal}
                      pageSize={PAGE_SIZE}
                      current={rechargesPage}
                      onChange={setRechargesPage}
                    />
                  </>
                )}

                {activeTab === 'consumptions' && (
                  <>
                    {consumptions.length === 0 ? (
                      <p className="billing-empty">暂无消费记录</p>
                    ) : (
                      <div className="billing-list">
                        {consumptions.map(c => (
                          <div key={c.id} className="billing-item">
                            <div>
                              <div className="billing-item-title">{c.description}</div>
                              <div className="billing-item-time">
                                {formatTime(c.createdAt)}
                                {c.metadata?.modelId && ` · ${c.metadata.modelId}`}
                              </div>
                            </div>
                            <div className="billing-item-amount">-¥{fenToYuan(c.amountFen)}</div>
                          </div>
                        ))}
                      </div>
                    )}
                    <Paginator
                      total={consumptionsTotal}
                      pageSize={PAGE_SIZE}
                      current={consumptionsPage}
                      onChange={setConsumptionsPage}
                    />
                  </>
                )}
              </section>
            </>
          )}
        </div>
      </main>
    </>
  );
}
