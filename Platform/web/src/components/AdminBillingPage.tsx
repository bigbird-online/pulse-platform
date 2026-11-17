import { useCallback, useEffect, useState } from 'react';
import { api, fenToYuan, formatTime } from '../api';
import type { AdminBillingSummary } from '../types';

export function AdminBillingPage() {
  const [summary, setSummary] = useState<AdminBillingSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api<AdminBillingSummary>('/api/admin/billing/summary');
      if (res.ok) {
        setSummary(res.data);
      } else {
        setError('获取数据失败');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '网络错误');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchSummary();
  }, [fetchSummary]);

  const ov = summary?.overview;
  const rc = summary?.recharges;

  return (
    <>
      <section className="hero" style={{ minHeight: '24vh', padding: '100px 24px 32px' }}>
        <div className="hero-inner">
          <p className="hero-eyebrow">管理后台</p>
          <h1 className="hero-title" style={{ fontSize: 'clamp(28px, 4vw, 40px)' }}>财务概览</h1>
          <p className="hero-subtitle">平台充值、消费与租户余额统计</p>
        </div>
      </section>

      <main className="main">
        <div className="main-inner" style={{ maxWidth: '720px' }}>
          {loading && !summary && (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-muted)' }}>
              加载中...
            </div>
          )}

          {error && (
            <div style={{ padding: '12px 16px', borderRadius: '12px', background: 'rgba(239,68,68,0.1)', color: '#f87171', fontSize: '14px', marginBottom: '16px' }}>
              {error}
            </div>
          )}

          {summary && (
            <>
              {/* Overview cards */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '20px' }}>
                <section className="card" style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '4px' }}>平台总余额</div>
                  <div style={{ fontSize: '28px', fontWeight: 900, color: 'var(--primary)' }}>
                    ¥{fenToYuan(ov?.totalBalanceFen ?? 0)}
                  </div>
                </section>
                <section className="card" style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '4px' }}>租户数</div>
                  <div style={{ fontSize: '28px', fontWeight: 900 }}>
                    {ov?.tenantCount ?? 0}
                    <span style={{ fontSize: '14px', color: 'var(--text-muted)', fontWeight: 400 }}> 活跃 {ov?.activeTenantCount ?? 0}</span>
                  </div>
                </section>
              </div>

              {/* Recharge stats */}
              <section className="card">
                <h2>充值统计</h2>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '12px' }}>
                  {[
                    { label: '今日', value: rc?.todayYuan ?? 0 },
                    { label: '本周', value: rc?.weekYuan ?? 0 },
                    { label: '本月', value: rc?.monthYuan ?? 0 },
                    { label: '累计', value: rc?.totalYuan ?? 0 },
                  ].map(s => (
                    <div key={s.label} style={{ textAlign: 'center' }}>
                      <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{s.label}</div>
                      <div style={{ fontSize: '18px', fontWeight: 700, color: '#22c55e' }}>¥{s.value.toFixed(2)}</div>
                    </div>
                  ))}
                </div>
                <div style={{ marginTop: '12px', textAlign: 'center' }}>
                  <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>累计消费</div>
                  <div style={{ fontSize: '18px', fontWeight: 700, color: '#f87171' }}>¥{fenToYuan(ov?.totalConsumedFen ?? 0)}</div>
                </div>
              </section>

              {/* Tenant list */}
              <section className="card">
                <h2>租户余额</h2>
                {(!summary.tenants || summary.tenants.length === 0) ? (
                  <p style={{ color: 'var(--text-muted)', fontSize: '14px', textAlign: 'center' }}>暂无租户</p>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {summary.tenants.map(t => (
                      <div key={t.tenantId} style={{
                        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                        padding: '12px 14px', borderRadius: '12px',
                        background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border-default)',
                      }}>
                        <div>
                          <div style={{ fontSize: '15px', fontWeight: 500 }}>{t.tenantId}</div>
                          <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                            充值 ¥{fenToYuan(t.totalRechargedFen)} · 消费 ¥{fenToYuan(t.totalConsumedFen)}
                          </div>
                        </div>
                        <div style={{
                          fontSize: '18px', fontWeight: 700,
                          color: t.balanceFen > 0 ? 'var(--primary)' : 'var(--text-muted)',
                        }}>
                          ¥{fenToYuan(t.balanceFen)}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              {/* Refresh */}
              <div style={{ textAlign: 'center', marginTop: '16px' }}>
                <button className="btn-secondary" onClick={() => void fetchSummary()} disabled={loading}>
                  {loading ? '刷新中...' : '刷新数据'}
                </button>
              </div>
            </>
          )}
        </div>
      </main>
    </>
  );
}
