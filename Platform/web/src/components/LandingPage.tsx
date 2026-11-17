import { useState } from 'react';

const CAPABILITIES = [
  {
    icon: '⚡',
    title: 'AI Agent 算力',
    desc: 'Claude + OpenRouter 多模型引擎，自动 fallback 切换，7×24 稳定运行。进程池管理、崩溃恢复、上下文续接，让 Agent 永远在线。',
  },
  {
    icon: '💬',
    title: '多通道接入',
    desc: '飞书 + Telegram 双通道，在已有的聊天工具中直接与 Agent 对话。零学习成本，发消息即用。',
  },
  {
    icon: '⏰',
    title: '定时任务',
    desc: '自动日报、夜巡巡检、数据采集。Agent 不只在被问时才工作——它会主动为你在后台执行计划任务。',
  },
  {
    icon: '🤝',
    title: '智能协作',
    desc: '多 Agent 协同工作，Work Item 追踪，任务交接与异常自动升级。每个 Agent 各司其职，形成完整的自动化闭环。',
  },
];

const SCENARIOS = [
  {
    icon: '🧑‍💼',
    title: '个人效率助手',
    desc: '日程管理、信息整理、知识问答、文件处理。让 Agent 成为你24小时在线的私人助理。',
  },
  {
    icon: '📢',
    title: '运营社群管理',
    desc: '成员触达、话术生成、转化追踪、信号录入。Agent 帮你管理社群，从线索到成交全程覆盖。',
  },
  {
    icon: '📊',
    title: '数据分析洞察',
    desc: '自动采集、定期报告、趋势预警。Agent 定时整理数据，把洞察主动推送到你的聊天窗口。',
  },
  {
    icon: '🔍',
    title: '自动化巡检',
    desc: '定时健康检查、异常告警、自主修复建议。Agent 在你睡觉时守护系统，有问题立即通知。',
  },
];

const PLANS = [
  {
    id: 'domestic',
    name: '国内模型',
    price: '经济型',
    highlight: false,
    features: ['DeepSeek（经 OpenRouter）', '全部通用 Agent', '飞书 + Telegram 双通道', '定时任务', '预付费按量扣减'],
    disabled: false,
  },
  {
    id: 'foreign',
    name: '国外模型',
    price: '高性能型',
    highlight: true,
    features: ['Claude Sonnet / Opus / Haiku', '全部通用 Agent', '飞书 + Telegram 双通道', '定时任务 + 智能协作', '预付费按量扣减'],
    disabled: true,
  },
];

const STEPS = [
  { num: '1', title: '充值开通', desc: '选择模型组，充值余额，完成支付' },
  { num: '2', title: '绑定飞书', desc: '创建飞书自建应用，填写凭证并配对' },
  { num: '3', title: '开始对话', desc: '在飞书中直接与你的 AI Agent 对话' },
];

export function LandingPage({ onSignup }: { onSignup: () => void }) {
  const [selectedPlan, setSelectedPlan] = useState<string | null>(null);

  return (
    <div className="landing">
      {/* Hero */}
      <section className="landing-hero">
        <div className="landing-hero-inner">
          <p className="landing-eyebrow">PULSE PLATFORM</p>
          <h1 className="landing-title">
            你的 AI Agent，
            <br />
            随时在线
          </h1>
          <p className="landing-subtitle">
            开通专属 AI Agent，在飞书和 Telegram 中直接对话。
            多模型算力、定时任务、智能协作——三步即可开始。
          </p>
          <button className="btn-primary landing-cta" onClick={onSignup}>
            立即开通
          </button>
        </div>
      </section>

      {/* Capabilities */}
      <section className="landing-section">
        <div className="landing-section-inner">
          <h2 className="landing-section-title">平台能力</h2>
          <p className="landing-section-subtitle">
            PULSE = 多通道网关 + AI 算力池 + 定时调度 + 协作框架
          </p>
          <div className="landing-grid landing-grid-4">
            {CAPABILITIES.map((c) => (
              <div className="landing-card" key={c.title}>
                <div className="landing-card-icon">{c.icon}</div>
                <h3>{c.title}</h3>
                <p>{c.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Scenarios */}
      <section className="landing-section landing-section-alt">
        <div className="landing-section-inner">
          <h2 className="landing-section-title">使用场景</h2>
          <p className="landing-section-subtitle">
            预配置通用 Agent，覆盖个人效率到团队运营的完整场景
          </p>
          <div className="landing-grid landing-grid-4">
            {SCENARIOS.map((s) => (
              <div className="landing-card" key={s.title}>
                <div className="landing-card-icon">{s.icon}</div>
                <h3>{s.title}</h3>
                <p>{s.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Onboarding Steps */}
      <section className="landing-section landing-section-alt">
        <div className="landing-section-inner">
          <h2 className="landing-section-title">三步开通</h2>
          <p className="landing-section-subtitle">
            从注册到对话，全程不到 5 分钟
          </p>
          <div className="landing-grid landing-grid-3">
            {STEPS.map((s) => (
              <div className="landing-card landing-step" key={s.num}>
                <div className="landing-step-num">{s.num}</div>
                <h3>{s.title}</h3>
                <p>{s.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section className="landing-section">
        <div className="landing-section-inner">
          <h2 className="landing-section-title">模型与定价</h2>
          <p className="landing-section-subtitle">
            预付费按量扣减，充多少用多少，随时充值
          </p>
          <div className="landing-grid landing-grid-2">
            {PLANS.map((p) => {
              const selected = selectedPlan === p.id;
              return (
                <div
                  className={`landing-card landing-pricing${selected ? ' landing-pricing-selected' : selectedPlan === null && p.highlight ? ' landing-pricing-featured' : ''}${p.disabled ? ' landing-pricing-disabled' : ''}`}
                  key={p.name}
                  onClick={() => { if (!p.disabled) setSelectedPlan(p.id); }}
                >
                  {p.disabled && <div className="landing-pricing-badge landing-pricing-badge-soon">即将开放</div>}
                  {!p.disabled && p.highlight && selectedPlan === null && <div className="landing-pricing-badge">推荐</div>}
                  <h3>{p.name}</h3>
                  <div className="landing-pricing-price">
                    <span className="landing-pricing-amount">{p.price}</span>
                  </div>
                  <ul className="landing-pricing-features">
                    {p.features.map((f) => (
                      <li key={f}>{f}</li>
                    ))}
                  </ul>
                  <button
                    className={p.disabled ? 'btn-secondary' : (selected ? 'btn-primary' : 'btn-secondary')}
                    disabled={p.disabled}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (p.disabled) return;
                      if (selected) {
                        onSignup();
                      } else {
                        setSelectedPlan(p.id);
                      }
                    }}
                  >
                    {p.disabled ? '即将开放' : (selected ? '立即开通' : '选择方案')}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </section>
    </div>
  );
}
