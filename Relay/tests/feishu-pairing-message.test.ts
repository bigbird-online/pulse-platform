// 飞书配对提示：受管实例保持 Platform 开通页原文，独立部署用中性文字。
import { describe, expect, it } from 'vitest';
import { feishuPairingMessage } from '../src/channels/feishu.js';

describe('feishuPairingMessage', () => {
  it('受管实例（设了 PULSE_RELAY_PATHS）沿用开通页 Step 3 原文', () => {
    const text = feishuPairingMessage('ABC123', { PULSE_RELAY_PATHS: '/srv/pulse/config/relay/paths.json' });
    expect(text).toContain('配对码：ABC123');
    expect(text).toContain('请回到 PULSE 开通页面 Step 3 粘贴此码完成绑定。');
    expect(text).toContain('/approve ABC123');
  });

  it('独立部署只给配对码与中性提示，不提 PULSE 开通页', () => {
    const text = feishuPairingMessage('ABC123', {});
    expect(text).toBe('\u{1F527} 您尚未绑定此助手。\n配对码：ABC123\n请把配对码发给管理员完成绑定。');
    expect(text).not.toContain('PULSE');
  });
});
