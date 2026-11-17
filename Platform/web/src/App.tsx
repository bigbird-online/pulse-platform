import { useCallback, useEffect, useRef, useState } from 'react';
import { api, baseUrlJoin, WEB_BASE } from './api';
import type { Capabilities, OrderStatus, PayScenarioKey, StepIdx } from './types';
import { NavBar } from './components/NavBar';
import { HeroHeader } from './components/HeroHeader';
import { StatusMessage } from './components/StatusMessage';
import { Footer } from './components/Footer';
import { ProgressBar } from './components/ProgressBar';
import { Step1PlanSelection } from './components/Step1PlanSelection';
import { Step2FeishuCredentials } from './components/Step2FeishuCredentials';
import { Step3PairingCode } from './components/Step3PairingCode';
import { Step4Success } from './components/Step4Success';
import { BalanceInfo } from './components/BalanceInfo';
import { LandingPage } from './components/LandingPage';
import { useWizardProgress } from './useWizardProgress';
import { BillingPage } from './components/BillingPage';
import { AdminBillingPage } from './components/AdminBillingPage';
import { RechargePage } from './components/RechargePage';
import './transitions.css';

type Page = 'landing' | 'signup' | 'billing' | 'admin-billing' | 'recharge';

function pageFromHash(): Page {
  const raw = window.location.hash.replace('#', '');
  const h = raw.split('?')[0]; // strip query params (e.g. ?token=...)
  if (h === 'billing') return 'billing';
  if (h === 'admin') return 'admin-billing';
  if (h === 'recharge') return 'recharge';
  return 'landing';
}

const PAGE_TITLES: Record<Page, string> = {
  landing: 'PULSE Platform',
  signup: '开通专属 AI Agent · PULSE Platform',
  billing: '余额与账单 · PULSE Platform',
  'admin-billing': '管理后台 · PULSE Platform',
  recharge: '充值 · PULSE Platform',
};

function useHashPage(): [Page, () => void, () => void, (p: Page) => void] {
  const [page, setPage] = useState<Page>(pageFromHash);

  // Sync page state when hash changes externally (e.g. billing → recharge link)
  useEffect(() => {
    const onHashChange = () => { setPage(pageFromHash()); };
    window.addEventListener('hashchange', onHashChange);
    return () => { window.removeEventListener('hashchange', onHashChange); };
  }, []);

  const goSignup = useCallback(() => { setPage('signup'); window.location.hash = ''; }, []);
  const goLanding = useCallback(() => { setPage('landing'); window.location.hash = ''; }, []);
  const goPage = useCallback((p: Page) => { setPage(p); window.location.hash = p === 'admin-billing' ? 'admin' : p === 'billing' ? 'billing' : p === 'recharge' ? 'recharge' : ''; }, []);
  return [page, goSignup, goLanding, goPage];
}

export function App() {
  const [page, goSignup, goLanding, goPage] = useHashPage();

  // Sync browser tab title with current page
  useEffect(() => {
    document.title = PAGE_TITLES[page] ?? 'PULSE Platform';
  }, [page]);

  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [step, setStep] = useState<StepIdx>(1);
  const [animatingStep, setAnimatingStep] = useState<StepIdx | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  // Step 1: phone + displayName + model group + top-up amount
  const [phone, setPhone] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [modelGroup, setModelGroup] = useState('domestic');
  const [topUpAmount, setTopUpAmount] = useState(100);
  const [customAmount, setCustomAmount] = useState('');
  const [orderId, setOrderId] = useState('');
  const [orderStatus, setOrderStatus] = useState<OrderStatus>('');
  const [subErr, setSubErr] = useState<string | null>(null);

  // Legacy QR payment state (still used for native payment flow)
  const [payScenario, setPayScenario] = useState<PayScenarioKey>('member');
  const [nativeLoading, setNativeLoading] = useState(false);
  const [nativeErr, setNativeErr] = useState<string | null>(null);
  const [nativeData, setNativeData] = useState<{ order_no: string; qrcode_base64: string } | null>(null);
  const [countdown, setCountdown] = useState<number | null>(null);

  // Step 2: feishu credentials
  const [feishuAppId, setFeishuAppId] = useState('');
  const [feishuAppSecret, setFeishuAppSecret] = useState('');
  const [tenantId, setTenantId] = useState('');
  const [agentId, setAgentId] = useState('');
  const [agentName, setAgentName] = useState('');

  // Step 3: pairing code
  const [pairingCode, setPairingCode] = useState('');
  const [paired, setPaired] = useState(false);

  const orderStatusTimer = useRef<number | null>(null);

  // ── Progress persistence ──────────────────────────────────────────────
  const { getStored, save, clear } = useWizardProgress();
  const restoredRef = useRef(false);

  // Restore progress on mount
  useEffect(() => {
    if (restoredRef.current) return;
    restoredRef.current = true;
    const stored = getStored();
    if (!stored) return;

    // Validate orderId still exists on server
    if (stored.orderId) {
      void api<{ status: string }>(`/internal/orders/${stored.orderId}`).then((r) => {
        if (!r.ok) {
          clear();
          return;
        }
        // Restore state
        setStep(stored.step);
        setPhone(stored.phone);
        setDisplayName(stored.displayName);
        setOrderId(stored.orderId);
        setOrderStatus(stored.orderStatus);
        if (stored.topUpAmount) setTopUpAmount(stored.topUpAmount);
        if (stored.modelGroup) setModelGroup(stored.modelGroup);
        setFeishuAppId(stored.feishuAppId);
        setTenantId(stored.tenantId);
        setAgentId(stored.agentId);
        setAgentName(stored.agentName);
      });
    } else {
      // No orderId, just restore basic fields
      setStep(stored.step);
      setPhone(stored.phone);
      setDisplayName(stored.displayName);
      if (stored.topUpAmount) setTopUpAmount(stored.topUpAmount);
      if (stored.modelGroup) setModelGroup(stored.modelGroup);
      setFeishuAppId(stored.feishuAppId);
      setTenantId(stored.tenantId);
      setAgentId(stored.agentId);
      setAgentName(stored.agentName);
    }
  }, [getStored, clear]);

  // Save progress on state changes
  useEffect(() => {
    if (!restoredRef.current) return; // Don't save before restore
    if (step === 4) {
      clear();
      return;
    }
    save({
      step,
      phone,
      displayName,
      orderId,
      orderStatus,
      topUpAmount,
      modelGroup,
      feishuAppId,
      tenantId,
      agentId,
      agentName,
    });
  }, [step, phone, displayName, orderId, orderStatus, topUpAmount, modelGroup, feishuAppId, tenantId, agentId, agentName, save, clear]);

  // ?test=1 URL param enables grayscale test options (e.g. ¥0.01 top-up)
  const testMode = typeof URLSearchParams !== 'undefined'
    && new URLSearchParams(window.location.search).get('test') === '1';

  const loadCaps = useCallback(async () => {
    const r = await api<Capabilities>('/internal/capabilities');
    if (r.ok) {
      // Filter out test-only presets when ?test=1 is absent
      if (!testMode && r.data.topUpPresets) {
        const isObjArray = (arr: unknown[]): arr is import('./types').PresetItem[] =>
          arr.length > 0 && typeof arr[0] === 'object' && arr[0] !== null && 'amount' in (arr[0] as object);
        if (isObjArray(r.data.topUpPresets as unknown[])) {
          // New format: PresetItem[]
          const filtered = (r.data.topUpPresets as import('./types').PresetItem[]).filter(p => p.amount >= 50);
          r.data.topUpPresets = filtered.length ? filtered : [
            { amount: 50, credit: 250, bonus: 200 },
            { amount: 100, credit: 500, bonus: 400 },
            { amount: 200, credit: 1000, bonus: 800 },
            { amount: 500, credit: 2500, bonus: 2000 },
          ];
        } else {
          // Legacy format: number[]
          const filtered = (r.data.topUpPresets as number[]).filter(v => v >= 50);
          r.data.topUpPresets = filtered.length ? filtered : [50, 100, 200, 500];
        }
        if ((r.data.minTopUpYuan ?? 50) < 50) r.data.minTopUpYuan = 50;
      }
      setCaps(r.data);
    }
    else
      setCaps({
        mockPay: false,
        provisionConfigured: false,
        nativePayment: false,
        paymentInstructions: '请使用微信扫码支付',
        paymentRefreshInterval: 30,
        paymentScenarios: {},
      });
  }, [testMode]);

  useEffect(() => {
    void loadCaps();
  }, [loadCaps]);

  // Set default model group from caps
  useEffect(() => {
    if (caps?.modelGroups?.length && !caps.modelGroups.find(g => g.id === modelGroup)) {
      setModelGroup(caps.modelGroups[0].id);
    }
  }, [caps?.modelGroups, modelGroup]);

  // Reset topUpAmount if it fell below minTopUpYuan after caps filtering
  useEffect(() => {
    const minTopUp = caps?.minTopUpYuan ?? 50;
    if (caps && topUpAmount < minTopUp) {
      const presets = caps.topUpPresets ?? [{ amount: 50 }, { amount: 100 }, { amount: 200 }, { amount: 500 }] as import('./types').PresetItem[];
      const firstAmount = typeof presets[0] === 'object' && presets[0] !== null && 'amount' in presets[0]
        ? (presets[0] as import('./types').PresetItem).amount
        : presets[0] as unknown as number;
      setTopUpAmount(firstAmount ?? 100);
      setCustomAmount('');
    }
  }, [caps, topUpAmount]);

  // Trigger step-entering animation when step changes
  useEffect(() => {
    setAnimatingStep(step);
  }, [step]);

  const handleStepAnimationEnd = useCallback(() => {
    setAnimatingStep(null);
  }, []);

  // ── Step 1: Create top-up order ─────────────────────────────────────────
  const createOrder = useCallback(async () => {
    setErr(null);
    setOkMsg(null);
    setBusy(true);
    setSubErr(null);
    try {
      const r = await api<{ orderId?: string; status?: string; error?: string }>('/internal/orders', {
        method: 'POST',
        body: JSON.stringify({
          phone: phone.trim(),
          displayName: displayName.trim(),
          topUpAmount,
          modelGroup,
        }),
      });
      if (!r.ok || !r.data.orderId) {
        const errMsg = r.data.error === 'not_allowlisted'
          ? '此手机号未在白名单内，请联系管理员加入。'
          : JSON.stringify(r.data);
        setSubErr(errMsg);
        return;
      }
      setOrderId(r.data.orderId);
      setOrderStatus('pending');
    } finally {
      setBusy(false);
    }
  }, [phone, displayName, topUpAmount, modelGroup]);

  // Legacy: QR code loading for native payment
  const loadNativeQr = useCallback(async () => {
    if (step !== 1) return;
    if (!orderId || !phone.trim()) return;
    if (!caps?.nativePayment) return;
    setNativeLoading(true);
    setNativeErr(null);
    try {
      const qs = new URLSearchParams({
        type: payScenario,
        phone: phone.trim(),
        internalOrderId: orderId,
      });
      const res = await fetch(baseUrlJoin(WEB_BASE, `/api/payment/create-order?${qs}`));
      const data = (await res.json()) as {
        success: boolean;
        error?: string;
        qrcode_base64?: string;
        order_no?: string;
      };

      const advanceIfOrderPaid = async (): Promise<boolean> => {
        const r = await api<{ status: 'pending' | 'paid' | 'failed' | 'refunded' }>(`/internal/orders/${orderId}`);
        if (r.ok && r.data.status === 'paid') {
          setOrderStatus('paid');
          setOkMsg('支付成功，进入下一步。');
          setStep(2);
          setCountdown(null);
          setNativeErr(null);
          return true;
        }
        return false;
      };

      if (!data.success) {
        const errRaw = data.error ?? '';
        const paidLike =
          errRaw === 'order_already_paid' ||
          errRaw === 'provider_order_already_paid' ||
          /该订单已支付/.test(errRaw);

        if (paidLike) {
          const advanced = await advanceIfOrderPaid();
          if (advanced) return;
          setNativeErr(null);
          setOkMsg('支付结果正在同步，请稍候几秒（页面会自动进入下一步）…');
          return;
        }

        setNativeErr(
          errRaw === 'payment_service_unconfigured'
            ? '未配置收银台：请设置 PLATFORM_PAYMENT_PROXY_URL 或 PLATFORM_NATIVE_PAY_MOCK=1。'
            : errRaw || '创建支付单失败',
        );
        setNativeData(null);
        return;
      }
      setNativeData({ order_no: data.order_no!, qrcode_base64: data.qrcode_base64! });
      setCountdown(caps.paymentRefreshInterval ?? 30);
    } catch (e) {
      setNativeErr(e instanceof Error ? e.message : '网络错误');
      setNativeData(null);
    } finally {
      setNativeLoading(false);
    }
  }, [step, orderId, phone, payScenario, caps]);

  useEffect(() => {
    if (step !== 1) return;
    if (!orderId || !caps?.nativePayment) return;
    void loadNativeQr();
  }, [step, orderId, caps?.nativePayment, loadNativeQr]);

  useEffect(() => {
    if (step !== 1) return;
    if (countdown === null || countdown <= 0) return;
    const t = window.setInterval(() => {
      setCountdown((x) => (x === null || x <= 0 ? x : x - 1));
    }, 1000);
    return () => window.clearInterval(t);
  }, [step, countdown]);

  useEffect(() => {
    if (step !== 1) return;
    if (countdown === null || countdown > 0) return;
    const iv = caps?.paymentRefreshInterval ?? 30;
    let cancelled = false;
    void (async () => {
      await loadNativeQr();
      if (!cancelled) setCountdown(iv);
    })();
    return () => { cancelled = true; };
  }, [step, countdown, loadNativeQr, caps?.paymentRefreshInterval]);

  // Poll order status while in step 1 with an open order; when paid, advance.
  const checkOrderPaid = useCallback(async () => {
    if (!orderId) return false;
    const r = await api<{ status: 'pending' | 'paid' | 'failed' | 'refunded' }>(`/internal/orders/${orderId}`);
    if (r.ok && r.data.status === 'paid') {
      setOrderStatus('paid');
      setOkMsg('支付成功，进入下一步。');
      setStep(2);
      setCountdown(null);
      return true;
    }
    return false;
  }, [orderId]);

  useEffect(() => {
    if (step !== 1 || !orderId || orderStatus === 'paid') return;
    void checkOrderPaid(); // immediate first check
    orderStatusTimer.current = window.setInterval(() => { void checkOrderPaid(); }, 3000) as unknown as number;
    return () => {
      if (orderStatusTimer.current !== null) {
        window.clearInterval(orderStatusTimer.current);
        orderStatusTimer.current = null;
      }
    };
  }, [step, orderId, orderStatus, checkOrderPaid]);

  // Once we've advanced past Step 1, stop payment polling/refresh UI artifacts.
  useEffect(() => {
    if (step === 1) return;
    setCountdown(null);
    setNativeLoading(false);
  }, [step]);

  const mockPay = useCallback(async () => {
    if (!orderId) return;
    setErr(null);
    setOkMsg(null);
    setBusy(true);
    try {
      const r = await api<{ status?: string; error?: string }>('/internal/mock-pay', {
        method: 'POST',
        body: JSON.stringify({ orderId }),
      });
      if (!r.ok) {
        setErr(r.data.error === 'mock_pay_disabled' ? '当前环境未开启模拟支付。' : JSON.stringify(r.data));
        return;
      }
      setOrderStatus('paid');
      setOkMsg('订单已标记为已支付（模拟）。');
      setStep(2);
    } finally {
      setBusy(false);
    }
  }, [orderId]);

  // ── Step 2 helpers ────────────────────────────────────────────────────
  const submitFeishuCredentials = useCallback(async () => {
    if (!orderId) return;
    setErr(null);
    setOkMsg(null);
    setBusy(true);
    try {
      const r = await api<{
        ok?: boolean;
        already?: boolean;
        tenantId?: string;
        agentId?: string;
        agentName?: string;
        error?: string;
        feishuCode?: number;
        feishuMsg?: string;
        message?: string;
      }>('/internal/provision', {
        method: 'POST',
        body: JSON.stringify({
          orderId,
          feishuAppId: feishuAppId.trim(),
          feishuAppSecret: feishuAppSecret.trim(),
        }),
      });
      if (!r.ok || !r.data.ok) {
        if (r.data.error === 'invalid_feishu_credentials') {
          setErr(`飞书凭证校验失败（${r.data.feishuCode ?? '?'}）：${r.data.feishuMsg ?? '请检查 App ID / Secret'}`);
        } else if (r.data.error === 'feishu_unreachable') {
          setErr('无法联通飞书开放平台，请检查服务器网络。');
        } else if (r.data.error === 'provision_unconfigured') {
          setErr('服务器未配置 PLATFORM_RELAY_CONFIG_PATH / PLATFORM_TENANTS_ROOT。');
        } else {
          setErr(
            r.data.message
              ?? r.data.error
              ?? (r.rawText ? `开通失败（HTTP ${r.status}）：${r.rawText}` : `开通失败（HTTP ${r.status}）`),
          );
        }
        return;
      }
      setTenantId(r.data.tenantId ?? '');
      setAgentId(r.data.agentId ?? '');
      setAgentName(r.data.agentName ?? '');
      setOkMsg(r.data.already ? '已开通（幂等）。' : 'Relay 已加载新 Bot，请到飞书完成最后一步。');
      setStep(3);
    } finally {
      setBusy(false);
    }
  }, [orderId, feishuAppId, feishuAppSecret]);

  // ── Step 3 helpers ────────────────────────────────────────────────────
  const submitPairingCode = useCallback(async () => {
    if (!orderId) return;
    setErr(null);
    setOkMsg(null);
    setBusy(true);
    try {
      const r = await api<{ ok?: boolean; error?: string; openId?: string; expectedAgentId?: string; detail?: string }>(
        '/internal/pair',
        { method: 'POST', body: JSON.stringify({ orderId, code: pairingCode.trim() }) },
      );
      if (!r.ok || !r.data.ok) {
        switch (r.data.error) {
          case 'pair_code_unknown':
            setErr('配对码无效或已过期，请重新让 Bot 生成。');
            break;
          case 'pair_agent_mismatch':
            setErr(`配对码不属于该 Bot（期望 ${r.data.expectedAgentId ?? agentId}）。`);
            break;
          case 'pair_relay_unreachable':
            setErr(`无法联通 Relay：${r.data.detail ?? '请检查 PLATFORM_RELAY_HTTP_URL'}`);
            break;
          case 'pair_unconfigured':
            setErr('服务器未配置 PLATFORM_RELAY_HTTP_URL。');
            break;
          default:
            setErr(r.data.detail ?? r.data.error ?? '配对失败');
        }
        return;
      }
      setPaired(true);
      setOkMsg(`绑定成功！现在可以在飞书直接对话 ${agentName || agentId}。`);
      setStep(4);
    } finally {
      setBusy(false);
    }
  }, [orderId, pairingCode, agentId, agentName]);

  const relayReload = useCallback(async () => {
    setErr(null);
    setOkMsg(null);
    setBusy(true);
    try {
      const r = await api<{ ok?: boolean; error?: string; detail?: string; message?: string; added?: string[]; total?: number }>(
        '/internal/relay/reload',
        { method: 'POST', body: JSON.stringify({}) },
      );
      if (!r.ok || !r.data.ok) {
        const e = r.data.error ?? 'reload_failed';
        setErr(
          e === 'relay_unconfigured'
            ? '服务器未配置 PLATFORM_RELAY_HTTP_URL（无法触发热加载）。'
            : e === 'relay_unreachable'
              ? `无法联通 Relay：${r.data.detail ?? ''}`.trim()
              : r.data.message ?? e,
        );
        return;
      }
      const added = Array.isArray(r.data.added) ? r.data.added : [];
      setOkMsg(`已触发 Relay 热加载：新增 ${added.length} 个 Bot（总计 ${r.data.total ?? 0}）。请回到飞书再发一条消息触发配对码。`);
    } finally {
      setBusy(false);
    }
  }, []);

  const recommendedFeishuBotName = displayName.trim()
    ? `${displayName.trim()}的pulse管理员`
    : '聪聪的pulse管理员';

  if (page === 'landing') {
    return (
      <div className="app">
        <NavBar page={page} onNavigate={goSignup} />
        <LandingPage onSignup={goSignup} />
        <Footer />
      </div>
    );
  }

  if (page === 'billing') {
    return (
      <div className="app">
        <NavBar page={page} onNavigate={goLanding} />
        <BillingPage onNavigate={goPage} />
        <Footer />
      </div>
    );
  }

  if (page === 'admin-billing') {
    return (
      <div className="app">
        <NavBar page={page} onNavigate={goLanding} />
        <AdminBillingPage />
        <Footer />
      </div>
    );
  }

  if (page === 'recharge') {
    return (
      <div className="app">
        <NavBar page={page} onNavigate={goLanding} />
        <RechargePage onNavigate={goPage} />
        <Footer />
      </div>
    );
  }

  return (
    <div className="app">
      <NavBar page={page} onNavigate={goLanding} />

      <HeroHeader />

      <main className="main">
        <div className="main-inner">
          <ProgressBar currentStep={step} />

          <div
            className={animatingStep === step ? 'step-entering' : ''}
            onAnimationEnd={handleStepAnimationEnd}
          >
            <Step1PlanSelection
              caps={caps}
              step={step}
              busy={busy}
              phone={phone}
              displayName={displayName}
              modelGroup={modelGroup}
              topUpAmount={topUpAmount}
              customAmount={customAmount}
              orderId={orderId}
              orderStatus={orderStatus}
              subErr={subErr}
              nativeData={nativeData}
              nativeErr={nativeErr}
              recommendedFeishuBotName={recommendedFeishuBotName}
              setPhone={setPhone}
              setDisplayName={setDisplayName}
              setModelGroup={setModelGroup}
              setTopUpAmount={setTopUpAmount}
              setCustomAmount={setCustomAmount}
              createOrder={createOrder}
              mockPay={mockPay}
              checkOrderPaid={checkOrderPaid}
            />

            <Step2FeishuCredentials
              step={step}
              busy={busy}
              feishuAppId={feishuAppId}
              feishuAppSecret={feishuAppSecret}
              tenantId={tenantId}
              agentId={agentId}
              recommendedFeishuBotName={recommendedFeishuBotName}
              setFeishuAppId={setFeishuAppId}
              setFeishuAppSecret={setFeishuAppSecret}
              submitFeishuCredentials={submitFeishuCredentials}
            />

            <Step3PairingCode
              step={step}
              busy={busy}
              pairingCode={pairingCode}
              agentId={agentId}
              agentName={agentName}
              setPairingCode={setPairingCode}
              submitPairingCode={submitPairingCode}
              relayReload={relayReload}
            />

            {paired ? (
              <>
                <Step4Success tenantId={tenantId} agentId={agentId} agentName={agentName} />
                <BalanceInfo topUpAmount={topUpAmount} modelGroup={modelGroup} />
              </>
            ) : null}
          </div>

          <StatusMessage error={err} success={okMsg} />
        </div>
      </main>

      <Footer />
    </div>
  );
}
