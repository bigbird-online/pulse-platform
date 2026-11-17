type Props = {
  topUpAmount: number;
  modelGroup: string;
};

const GROUP_LABELS: Record<string, string> = {
  domestic: '国内模型',
  foreign: '国外模型',
};

export function BalanceInfo({ topUpAmount, modelGroup }: Props) {
  const groupLabel = GROUP_LABELS[modelGroup] ?? modelGroup;

  return (
    <section className="card" style={{ marginTop: '16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
        <span style={{ color: '#22c55e', fontSize: '18px' }}>&#x1F7E2;</span>
        <strong>预付费账户 · 已激活</strong>
      </div>
      <p style={{ margin: '4px 0', fontSize: '15px' }}>
        当前余额：<strong>¥{topUpAmount}</strong>
      </p>
      <p style={{ margin: '4px 0', fontSize: '14px', color: 'var(--text-secondary)' }}>
        模型组：{groupLabel} · 按量扣减
      </p>
      <p style={{ margin: '12px 0 4px', fontSize: '13px', color: 'var(--text-muted)' }}>
        余额不足时，在飞书中发送 /recharge 即可充值。
      </p>
    </section>
  );
}
