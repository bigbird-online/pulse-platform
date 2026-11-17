type Props = {
  step: number;
  busy: boolean;
  pairingCode: string;
  agentId: string;
  agentName: string;
  setPairingCode: (v: string) => void;
  submitPairingCode: () => void;
  relayReload: () => void;
};

export function Step3PairingCode({
  step,
  busy,
  pairingCode,
  setPairingCode,
  submitPairingCode,
  relayReload,
}: Props) {
  if (step < 3) return null;

  return (
    <section className="card">
      <h2>3 · 飞书配对码</h2>
      <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
        现在打开飞书，添加你刚才创建的应用为联系人，发送任意一条消息。Bot 会回复一条以
        <code className="mono"> 配对码：</code>开头的 6 位字母数字（排除易混字符）。把它粘贴下来。
      </p>
      <div style={{ marginTop: '10px', marginBottom: '14px', padding: '10px 12px', border: '1px solid var(--border-default)', borderRadius: '10px', background: 'rgba(255,255,255,0.02)' }}>
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          如果几分钟后 Bot 仍未回复，请点击重试。
        </div>
        <div className="row" style={{ marginTop: '10px' }}>
          <button type="button" className="btn-secondary" disabled={busy} onClick={relayReload}>
            重试
          </button>
        </div>
      </div>
      <label htmlFor="pairingCode">配对码</label>
      <input
        id="pairingCode"
        value={pairingCode}
        disabled={step > 3}
        onChange={(e) => setPairingCode(e.target.value.toUpperCase())}
        placeholder="例如 AB23CD"
        maxLength={6}
      />
      <div className="char-count">{pairingCode.length}/6</div>
      {step === 3 ? (
        <div className="row">
          <button
            type="button"
            className="btn-primary"
            disabled={busy || pairingCode.trim().length !== 6}
            onClick={submitPairingCode}
          >
            完成绑定
          </button>
        </div>
      ) : null}
    </section>
  );
}
