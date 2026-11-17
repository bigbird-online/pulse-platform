import { useEffect, useState } from 'react';
import { api } from '../api';

type Props = {
  tenantId: string;
  agentId: string;
  agentName: string;
};

export function Step4Success({ tenantId, agentName }: Props) {
  const [dashboardUrl, setDashboardUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!tenantId) return;
    void (async () => {
      try {
        const res = await api<{ token: string; dashboardUrl: string }>(
          '/api/auth/dashboard-token',
          { method: 'POST', body: JSON.stringify({ tenantId }) },
        );
        if (res.ok && res.data.dashboardUrl) {
          setDashboardUrl(res.data.dashboardUrl);
        }
      } catch {
        // non-critical: dashboard link is a convenience, not essential
      }
    })();
  }, [tenantId]);

  return (
    <section className="card" style={{ borderColor: 'var(--color-primary-500)' }}>
      <h2>&#10003; 完成</h2>
      <p style={{ marginTop: 0 }}>
        租户 <code className="mono">{tenantId}</code> 已绑定到飞书。可直接在飞书内与 <strong>{agentName}</strong> 对话。
      </p>
      <p style={{ marginTop: '8px', color: 'var(--text-muted)', fontSize: '13px' }}>
        如果 Bot 无回应，请在飞书开放平台检查事件订阅是否已启用。
      </p>
      {dashboardUrl && (
        <div style={{ marginTop: '16px', padding: '12px 16px', borderRadius: '12px', background: 'rgba(99,102,241,0.08)', border: '1px solid rgba(99,102,241,0.2)' }}>
          <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>📊 账户中心</div>
          <p style={{ fontSize: '12px', color: 'var(--text-muted)', margin: '0 0 10px' }}>
            查看余额、消费明细和充值入口（链接 24 小时内有效）
          </p>
          <a
            href={dashboardUrl}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              display: 'inline-block', padding: '8px 20px', borderRadius: '980px',
              background: 'var(--color-primary-500)', color: '#fff',
              fontSize: '13px', fontWeight: 600, textDecoration: 'none',
            }}
          >
            打开账户中心
          </a>
        </div>
      )}
    </section>
  );
}
