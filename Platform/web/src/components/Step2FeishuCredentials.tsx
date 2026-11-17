type Props = {
  step: number;
  busy: boolean;
  feishuAppId: string;
  feishuAppSecret: string;
  tenantId: string;
  agentId: string;
  recommendedFeishuBotName: string;
  setFeishuAppId: (v: string) => void;
  setFeishuAppSecret: (v: string) => void;
  submitFeishuCredentials: () => void;
};

export function Step2FeishuCredentials({
  step,
  busy,
  feishuAppId,
  feishuAppSecret,
  tenantId,
  agentId,
  recommendedFeishuBotName,
  setFeishuAppId,
  setFeishuAppSecret,
  submitFeishuCredentials,
}: Props) {
  if (step < 2) return null;

  return (
    <section className="card">
      <h2>2 · 飞书 App 凭证</h2>
      <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
        请前往
        <a
          href="https://open.feishu.cn/app"
          target="_blank"
          rel="noreferrer"
          style={{ color: 'var(--color-primary-500)', textDecoration: 'underline', margin: '0 4px' }}
        >
          飞书开放平台
        </a>
        创建自建应用（建议命名为 <code className="mono">{recommendedFeishuBotName}</code>），并复制
        <code className="mono"> App ID </code>与<code className="mono"> App Secret </code>到下方。系统会验证你的凭证并自动完成配置。
      </p>
      <label htmlFor="feishuAppId">App ID</label>
      <input
        id="feishuAppId"
        value={feishuAppId}
        disabled={step > 2}
        onChange={(e) => setFeishuAppId(e.target.value)}
        placeholder="cli_a1b2c3d4..."
      />
      <label htmlFor="feishuAppSecret">App Secret</label>
      <input
        id="feishuAppSecret"
        type="password"
        value={feishuAppSecret}
        disabled={step > 2}
        onChange={(e) => setFeishuAppSecret(e.target.value)}
        autoComplete="off"
      />
      {step === 2 ? (
        <div className="row">
          <button
            type="button"
            className="btn-primary"
            disabled={busy || !feishuAppId.trim() || !feishuAppSecret.trim()}
            onClick={submitFeishuCredentials}
          >
            验证并继续
          </button>
        </div>
      ) : (
        <p style={{ marginTop: '14px', marginBottom: 0, fontSize: '14px', color: 'var(--text-secondary)' }}>
          已生成租户：<code className="mono">{tenantId}</code> · Agent <code className="mono">{agentId}</code>
        </p>
      )}
    </section>
  );
}
