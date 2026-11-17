import { describe, it, expect } from 'vitest';
import { mergeStreamingText, FeishuStreamingCardSession } from '../src/feishu-streaming-card.js';

describe('mergeStreamingText', () => {
  it('returns next when no previous', () => {
    expect(mergeStreamingText(undefined, 'hello')).toBe('hello');
  });

  it('treats cumulative CC chunks', () => {
    expect(mergeStreamingText('hel', 'hello')).toBe('hello');
  });

  it('merges overlap boundary', () => {
    expect(mergeStreamingText('这', '这是')).toBe('这是');
  });

  it('appends fragmented delta', () => {
    expect(mergeStreamingText('ab', 'cd')).toBe('abcd');
  });
});

describe('FeishuStreamingCardSession append mode', () => {
  it('preserves every cumulative character received inside one throttle window', async () => {
    const mockClient = {
      im: { message: { create: async () => ({ code: 0, data: { message_id: 'uat-msg' } }) } },
    };
    const session = new FeishuStreamingCardSession(
      { appId: 'uat-stream', appSecret: 'test' },
      mockClient as any,
    );
    const updateContents: string[] = [];
    let failContentOnce = true;
    const originalFetch = global.fetch;
    global.fetch = async (url: string | URL, init?: RequestInit) => {
      const urlStr = url.toString();
      if (urlStr.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'uat-token', expire: 7200 }) } as any;
      }
      if (urlStr.includes('/cardkit/v1/cards') && !urlStr.includes('/elements/') && !urlStr.includes('/settings')) {
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'uat-card' } }) } as any;
      }
      if (urlStr.includes('/elements/content/content')) {
        if (failContentOnce) {
          failContentOnce = false;
          return { ok: false, status: 503, text: async () => 'temporary' } as any;
        }
        const body = JSON.parse(String(init?.body)) as { content: string };
        updateContents.push(body.content);
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    };

    try {
      await session.start('uat-open-id');
      const expected = '我已继续处理；现在逐字符流式输出。\n第二行也必须完整，不得只剩标点：abcdefghijklmnopq';
      const characters = Array.from(expected);
      for (let index = 1; index <= characters.length; index += 1) {
        const cumulative = characters.slice(0, index).join('');
        if (index === 1) await expect(session.update(cumulative)).rejects.toThrow('Card update HTTP 503');
        else await session.update(cumulative);
      }

      expect(session.getCurrentText()).toBe(expected);
      await new Promise((resolve) => setTimeout(resolve, 1600));
      expect(updateContents.at(-1)).toBe(expected);
      await session.close(expected);
      expect(updateContents.at(-1)).toBe(expected);
    } finally {
      global.fetch = originalFetch;
    }
  });

  it('should append delta correctly in append mode', async () => {
    // Mock client
    const mockClient = {
      im: {
        message: {
          create: async () => ({ code: 0, data: { message_id: 'test-msg-123' } }),
        },
      },
    };

    const session = new FeishuStreamingCardSession(
      { appId: 'test', appSecret: 'test' },
      mockClient as any
    );

    // Mock fetch
    const updateContents: string[] = [];
    const originalFetch = global.fetch;
    global.fetch = async (url: string | URL, init?: RequestInit) => {
      const urlStr = url.toString();
      if (urlStr.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'mock-token', expire: 7200 }) } as any;
      }
      if (urlStr.includes('/cardkit/v1/cards') && !urlStr.includes('/elements/')) {
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'test-card-123' } }) } as any;
      }
      if (urlStr.includes('/elements/content/content')) {
        const body = init?.body as string;
        if (body) {
          const parsed = JSON.parse(body);
          updateContents.push(parsed.content);
        }
        return { ok: true, json: async () => ({ code: 0 }) } as any;
      }
      if (urlStr.includes('/settings')) {
        return { ok: true, json: async () => ({ code: 0 }) } as any;
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    };

    try {
      await session.start('test-open-id');

      // Simulate CC streaming with sufficient delay between calls
      // First chunk - small text should force update
      await session.update('Hello');
      
      // Wait for any pending flush
      await new Promise(resolve => setTimeout(resolve, 100));
      
      // Second chunk - large enough to trigger immediate update (over 18 chars delta)
      await session.update('Hello, this is a longer message to trigger immediate update');
      
      // Third chunk
      await session.update('Hello, this is a longer message to trigger immediate update!');
      
      // Wait for all pending updates
      await new Promise(resolve => setTimeout(resolve, 2000));
      
      // Close to flush any remaining pending text
      await session.close();

      // Verify we have updates and content grows
      expect(updateContents.length).toBeGreaterThanOrEqual(1);
      expect(updateContents[0]).toBe('Hello');
      
      // Each update should contain progressively more content (append mode)
      for (let i = 1; i < updateContents.length; i++) {
        expect(updateContents[i].length).toBeGreaterThanOrEqual(updateContents[i - 1].length);
      }
      
      // Final content should include all parts
      const finalContent = updateContents[updateContents.length - 1];
      expect(finalContent).toContain('Hello');
      expect(finalContent).toContain('!');
    } finally {
      global.fetch = originalFetch;
    }
  });

  it('should handle duplicate updates gracefully', async () => {
    const mockClient = {
      im: {
        message: {
          create: async () => ({ code: 0, data: { message_id: 'test-msg-456' } }),
        },
      },
    };

    const session = new FeishuStreamingCardSession(
      { appId: 'test', appSecret: 'test' },
      mockClient as any
    );

    const updateContents: string[] = [];
    const originalFetch = global.fetch;
    global.fetch = async (url: string | URL, init?: RequestInit) => {
      const urlStr = url.toString();
      if (urlStr.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'mock-token', expire: 7200 }) } as any;
      }
      if (urlStr.includes('/cardkit/v1/cards') && !urlStr.includes('/elements/')) {
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'test-card-456' } }) } as any;
      }
      if (urlStr.includes('/elements/content/content')) {
        const body = init?.body as string;
        if (body) {
          const parsed = JSON.parse(body);
          updateContents.push(parsed.content);
        }
        return { ok: true, json: async () => ({ code: 0 }) } as any;
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    };

    try {
      await session.start('test-open-id');

      // First update
      await session.update('Same text that is long enough to trigger immediate update');
      await new Promise(resolve => setTimeout(resolve, 100));
      const count1 = updateContents.length;

      // Duplicate update - should not trigger new API call
      await session.update('Same text that is long enough to trigger immediate update');
      await new Promise(resolve => setTimeout(resolve, 100));
      const count2 = updateContents.length;

      // Should not make additional call for duplicate
      expect(count2).toBe(count1);
    } finally {
      global.fetch = originalFetch;
    }
  });

  it('creates and closes a clean streaming card without copy controls or a replacement-card update', async () => {
    let initialCard: Record<string, unknown> | undefined;
    let replacementUpdates = 0;
    let settingsClosed = false;
    const mockClient = {
      im: { message: { create: async () => ({ code: 0, data: { message_id: 'clean-card-msg' } }) } },
    };
    const session = new FeishuStreamingCardSession(
      { appId: 'clean-card', appSecret: 'test' },
      mockClient as any,
    );
    const originalFetch = global.fetch;
    global.fetch = async (url: string | URL, init?: RequestInit) => {
      const target = url.toString();
      if (target.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'clean-token', expire: 7200 }) } as any;
      }
      if (target.endsWith('/cardkit/v1/cards') && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as { data: string };
        initialCard = JSON.parse(body.data) as Record<string, unknown>;
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'clean-card-id' } }) } as any;
      }
      if (target.endsWith('/cardkit/v1/cards/clean-card-id') && init?.method === 'PUT') replacementUpdates += 1;
      if (target.endsWith('/settings')) settingsClosed = true;
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    };

    try {
      await session.start('clean-open-id');
      await session.update('```ts\nconst answer = 42;\n```');
      await expect(session.close('```ts\nconst answer = 42;\n```')).resolves.toBeUndefined();
      const elements = (initialCard?.body as { elements?: Record<string, unknown>[] } | undefined)?.elements ?? [];
      expect(elements).toEqual([{ tag: 'markdown', content: '▶️ 已收到，开始处理…', element_id: 'content' }]);
      expect(JSON.stringify(initialCard)).not.toMatch(/relay_copy|copy_btn|发送可复制/);
      expect(replacementUpdates).toBe(0);
      expect(settingsClosed).toBe(true);
      expect(session.isClosed()).toBe(true);
    } finally {
      global.fetch = originalFetch;
    }
  });

  it('reports isClosed only after close() so stale sessions can be discarded', async () => {
    const mockClient = {
      im: { message: { create: async () => ({ code: 0, data: { message_id: 'test-msg-789' } }) } },
    };
    const session = new FeishuStreamingCardSession(
      { appId: 'test', appSecret: 'test' },
      mockClient as any
    );
    const originalFetch = global.fetch;
    global.fetch = async (url: string | URL) => {
      const urlStr = url.toString();
      if (urlStr.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'mock-token', expire: 7200 }) } as any;
      }
      return { ok: true, json: async () => ({ code: 0, data: { card_id: 'test-card-789' } }) } as any;
    };
    try {
      expect(session.isClosed()).toBe(false);
      await session.start('test-open-id');
      await session.update('hi');
      expect(session.isClosed()).toBe(false);
      await session.close();
      // After close, the session must report closed so onAssistant drops it and
      // opens a fresh card instead of PUTting into the now-finalized (older) card.
      expect(session.isClosed()).toBe(true);
    } finally {
      global.fetch = originalFetch;
    }
  });
});

describe('FeishuStreamingCardSession heartbeat stop-on-closed', () => {
  it('keeps the current shared status visible without legacy elapsed-time or tool-detail text', async () => {
    const mockClient = {
      im: { message: { create: async () => ({ code: 0, data: { message_id: 'status-message' } }) } },
    };
    const session = new FeishuStreamingCardSession(
      { appId: 'shared-status', appSecret: 'test' },
      mockClient as any,
    );
    let initialContent = '';
    const heartbeatContents: string[] = [];
    const originalFetch = global.fetch;
    global.fetch = async (url: string | URL, init?: RequestInit) => {
      const target = url.toString();
      if (target.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'status-token', expire: 7200 }) } as any;
      }
      if (target.endsWith('/cardkit/v1/cards') && init?.method === 'POST') {
        const request = JSON.parse(String(init.body)) as { data: string };
        const card = JSON.parse(request.data) as { body?: { elements?: Array<{ content?: string }> } };
        initialContent = card.body?.elements?.[0]?.content ?? '';
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'status-card' } }) } as any;
      }
      if (target.includes('/elements/content/content')) {
        const request = JSON.parse(String(init?.body)) as { content: string; uuid?: string };
        if (request.uuid?.startsWith('h_')) heartbeatContents.push(request.content);
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    };

    try {
      await session.start('status-open-id');
      session.setPulseLine('🔧 Bash(private command detail)');
      await new Promise((resolve) => setTimeout(resolve, 50));

      expect(initialContent).toBe('▶️ 已收到，开始处理…');
      expect(heartbeatContents).toEqual(['▶️ 已收到，开始处理…']);
      expect(heartbeatContents.join('\n')).not.toMatch(/任务执行中|已 \d+m\d+s|🔧|private command detail/);
      session.dispose();
    } finally {
      global.fetch = originalFetch;
    }
  });

  // 2026-08-06 回归钉子：服务端回 "streaming mode is closed" 后必须停心跳。
  //
  // 原行为：pushHeartbeat 只 throw，调用方 catch 后仅打一行 warn，closed 从不
  // 置位 → setInterval 每 tick 重打、每次必失败 → 同一条 warn 每秒刷数十次，
  // 占满事件循环 → watcher 打 127.0.0.1:8787/internal/heartbeat 超时 → 判
  // 「探针总监控失效」→ 推 🔴 给大鸟。一个纯内部空转 bug，表现是骚扰大鸟
  // （08-06 两次：16:25 / 18:39）。心跳路径此前零测试覆盖，故此 bug 活到今天。
  //
  // 本测试先撤掉修复验证过能变红，才留下来 —— 绿灯本身不算保护。
  it('stops streaming heartbeats without closing the active turn after expiry', async () => {
    const mockClient = {
      im: { message: { create: async () => ({ code: 0, data: { message_id: 'm1' } }) } },
    };
    const session = new FeishuStreamingCardSession(
      { appId: 'test', appSecret: 'test' },
      mockClient as any
    );

    let heartbeatPuts = 0;
    const originalFetch = global.fetch;
    global.fetch = async (url: string | URL, init?: RequestInit) => {
      const urlStr = url.toString();
      if (urlStr.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 't', expire: 7200 }) } as any;
      }
      if (urlStr.includes('/cardkit/v1/cards') && !urlStr.includes('/elements/')) {
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'c1' } }) } as any;
      }
      if (urlStr.includes('/elements/content/content')) {
        // 心跳帧的 uuid 带 h_ 前缀，据此与普通内容更新区分
        const body = init?.body as string | undefined;
        const isHeartbeat = !!body && JSON.parse(body).uuid?.startsWith('h_');
        if (isHeartbeat) {
          heartbeatPuts += 1;
          return { ok: true, json: async () => ({ code: 1, msg: 'streaming mode is closed; ' }) } as any;
        }
        return { ok: true, json: async () => ({ code: 0 }) } as any;
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    };

    try {
      await session.start('chat-1');
      expect(session.isClosed()).toBe(false);

      // setPulseLine 是触发 pushHeartbeat 的公开入口
      session.setPulseLine('working...');
      await new Promise((r) => setTimeout(r, 50));

      // Expiry stops streaming heartbeats, not progress on the same card entity.
      expect(heartbeatPuts).toBeGreaterThan(0);
      expect(session.isClosed()).toBe(false);

      // 关闭后不得再打服务端
      const before = heartbeatPuts;
      session.setPulseLine('still working...');
      await new Promise((r) => setTimeout(r, 100));
      expect(heartbeatPuts).toBe(before);
      await session.update("Still making real progress.");
      await session.close("Final answer.");
      expect(session.isClosed()).toBe(true);
    } finally {
      global.fetch = originalFetch;
    }
  });
});
