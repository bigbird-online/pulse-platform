import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  resolveVisionConfig,
  describeImages,
  extractImageBlocks,
  replaceImagesWithText,
} from '../src/vision-proxy.js';
import type { ImageContentBlock } from '../src/vision-proxy.js';

// --- resolveVisionConfig ---

describe('resolveVisionConfig', () => {
  const origEnv = { ...process.env };

  afterEach(() => {
    // Restore env
    for (const key of Object.keys(process.env)) {
      if (!(key in origEnv)) delete process.env[key];
    }
    Object.assign(process.env, origEnv);
  });

  it('returns null when no config provided and no env set', () => {
    delete process.env['RELAY_VISION_MODEL'];
    expect(resolveVisionConfig()).toBeNull();
    expect(resolveVisionConfig(undefined)).toBeNull();
  });

  it('resolves from agent config alone', () => {
    delete process.env['RELAY_VISION_MODEL'];
    const result = resolveVisionConfig({
      model: 'vision-model',
      baseUrl: 'https://vision.api.example.com',
      apiKey: 'sk-agent-key',
      systemPrompt: 'Describe the image',
    });
    expect(result).not.toBeNull();
    expect(result!.model).toBe('vision-model');
    expect(result!.baseUrl).toBe('https://vision.api.example.com');
    expect(result!.apiKey).toBe('sk-agent-key');
    expect(result!.systemPrompt).toBe('Describe the image');
  });

  it('resolves from env when no agent config', () => {
    process.env['RELAY_VISION_MODEL'] = 'vision-model-env';
    process.env['RELAY_VISION_BASE_URL'] = 'https://env.api.example.com';
    process.env['RELAY_VISION_API_KEY'] = 'sk-env-key';
    process.env['RELAY_VISION_SYSTEM_PROMPT'] = 'Env prompt';

    const result = resolveVisionConfig();
    expect(result).not.toBeNull();
    expect(result!.model).toBe('vision-model-env');
    expect(result!.baseUrl).toBe('https://env.api.example.com');
    expect(result!.apiKey).toBe('sk-env-key');
    expect(result!.systemPrompt).toBe('Env prompt');
  });

  it('agent config overrides env', () => {
    process.env['RELAY_VISION_MODEL'] = 'env-model';
    process.env['RELAY_VISION_BASE_URL'] = 'https://env.example.com';
    process.env['RELAY_VISION_API_KEY'] = 'env-key';

    const result = resolveVisionConfig({
      model: 'agent-model',
      apiKey: 'agent-key',
    });
    expect(result).not.toBeNull();
    expect(result!.model).toBe('agent-model');
    // apiKey from agent overrides env
    expect(result!.apiKey).toBe('agent-key');
    // baseUrl falls back to env
    expect(result!.baseUrl).toBe('https://env.example.com');
  });

  it('returns empty baseUrl/apiKey when neither agent nor env provides them', () => {
    process.env['RELAY_VISION_MODEL'] = 'vision-model';
    delete process.env['RELAY_VISION_BASE_URL'];
    delete process.env['RELAY_VISION_API_KEY'];

    const result = resolveVisionConfig();
    expect(result).not.toBeNull();
    expect(result!.baseUrl).toBe('');
    expect(result!.apiKey).toBe('');
  });

  it('uses default system prompt when none configured', () => {
    process.env['RELAY_VISION_MODEL'] = 'vision-model';
    delete process.env['RELAY_VISION_SYSTEM_PROMPT'];

    const result = resolveVisionConfig();
    expect(result).not.toBeNull();
    expect(result!.systemPrompt).toContain('描述');
  });
});

// --- extractImageBlocks ---

describe('extractImageBlocks', () => {
  it('extracts image blocks from mixed content', () => {
    const content = [
      { type: 'text', text: 'Hello' },
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'abc' } },
      { type: 'text', text: 'World' },
    ];
    const images = extractImageBlocks(content);
    expect(images).toHaveLength(1);
    expect(images[0]!.type).toBe('image');
  });

  it('returns empty array when no images', () => {
    const content = [
      { type: 'text', text: 'Hello' },
      { type: 'text', text: 'World' },
    ];
    expect(extractImageBlocks(content)).toHaveLength(0);
  });

  it('extracts multiple images', () => {
    const content = [
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'a' } },
      { type: 'text', text: 'between' },
      { type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: 'b' } },
    ];
    const images = extractImageBlocks(content);
    expect(images).toHaveLength(2);
  });

  it('skips null and non-object entries', () => {
    const content = [null, undefined, 'string', 42, { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'x' } }];
    const images = extractImageBlocks(content);
    expect(images).toHaveLength(1);
  });
});

// --- replaceImagesWithText ---

describe('replaceImagesWithText', () => {
  it('preserves existing text blocks and appends description', () => {
    const content = [
      { type: 'text', text: 'Hello' },
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'abc' } },
      { type: 'text', text: 'World' },
    ];
    const result = replaceImagesWithText(content, 'A beautiful sunset');
    expect(result).toHaveLength(3);
    expect(result[0]).toEqual({ type: 'text', text: 'Hello' });
    expect(result[1]).toEqual({ type: 'text', text: 'World' });
    expect(result[2]).toEqual({ type: 'text', text: '[用户发送了一张图片，以下是图片的详细描述：]\nA beautiful sunset\n[请根据以上图片描述内容来理解和回答用户的问题。]' });
  });

  it('works with only image blocks', () => {
    const content = [
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'abc' } },
    ];
    const result = replaceImagesWithText(content, 'A chart');
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ type: 'text', text: '[用户发送了一张图片，以下是图片的详细描述：]\nA chart\n[请根据以上图片描述内容来理解和回答用户的问题。]' });
  });

  it('works with empty content', () => {
    const result = replaceImagesWithText([], 'Nothing');
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ type: 'text', text: '[用户发送了一张图片，以下是图片的详细描述：]\nNothing\n[请根据以上图片描述内容来理解和回答用户的问题。]' });
  });
});

// --- describeImages (Anthropic format) ---

describe('describeImages', () => {
  const mockImageBlock: ImageContentBlock = {
    type: 'image',
    source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' },
  };

  const config = {
    model: 'claude-3-haiku',
    baseUrl: 'https://api.example.com',
    apiKey: 'test-key',
    systemPrompt: 'Describe this image',
  };

  const fallbackAuth = {
    baseUrl: 'https://fallback.example.com',
    apiKey: 'fallback-key',
  };

  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch');
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  it('calls Anthropic API and returns description', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        content: [{ type: 'text', text: 'A sunset over the ocean' }],
      }),
    } as Response);

    const result = await describeImages([mockImageBlock], config, fallbackAuth);
    expect(result).toBe('A sunset over the ocean');

    // Verify API call uses Anthropic format
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0]!;
    expect(url).toBe('https://api.example.com/v1/messages');
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body.model).toBe('claude-3-haiku');
    expect(body.max_tokens).toBe(1024);
    // source.base64 format, not image_url
    expect(body.messages[0].content[0].type).toBe('image');
    expect(body.messages[0].content[0].source.type).toBe('base64');
    // x-api-key auth
    expect((init as RequestInit).headers).toHaveProperty('x-api-key', 'test-key');
  });

  it('uses fallback auth when config has empty credentials', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        content: [{ type: 'text', text: 'Description' }],
      }),
    } as Response);

    const noAuthConfig = { ...config, baseUrl: '', apiKey: '' };
    await describeImages([mockImageBlock], noAuthConfig, fallbackAuth);

    const [, init] = fetchSpy.mock.calls[0]!;
    expect((init as RequestInit).headers).toHaveProperty('x-api-key', 'fallback-key');
    expect(fetchSpy.mock.calls[0]![0]).toBe('https://fallback.example.com/v1/messages');
  });

  it('returns placeholder on HTTP error', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: false,
      status: 500,
      text: async () => 'Internal Server Error',
    } as Response);

    const result = await describeImages([mockImageBlock], config, fallbackAuth);
    expect(result).toContain('描述失败');
    expect(result).toContain('HTTP 500');
  });

  it('returns placeholder when vision model returns empty content', async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ content: [] }),
    } as Response);

    const result = await describeImages([mockImageBlock], config, fallbackAuth);
    expect(result).toContain('无法识别');
  });

  it('returns placeholder on network error', async () => {
    fetchSpy.mockRejectedValueOnce(new Error('Network timeout'));

    const result = await describeImages([mockImageBlock], config, fallbackAuth);
    expect(result).toContain('描述失败');
    expect(result).toContain('Network timeout');
  });

  it('handles multiple images with numbered labels', async () => {
    fetchSpy
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ content: [{ type: 'text', text: 'First image' }] }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ content: [{ type: 'text', text: 'Second image' }] }),
      } as Response);

    const secondImage: ImageContentBlock = {
      type: 'image',
      source: { type: 'base64', media_type: 'image/jpeg', data: 'dGVzdA==' },
    };
    const result = await describeImages([mockImageBlock, secondImage], config, fallbackAuth);
    expect(result).toContain('**图片 1:**');
    expect(result).toContain('First image');
    expect(result).toContain('**图片 2:**');
    expect(result).toContain('Second image');
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('continues on partial failure with multiple images', async () => {
    fetchSpy
      .mockResolvedValueOnce({
        ok: false,
        status: 429,
        text: async () => 'Rate limited',
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ content: [{ type: 'text', text: 'Success' }] }),
      } as Response);

    const secondImage: ImageContentBlock = {
      type: 'image',
      source: { type: 'base64', media_type: 'image/jpeg', data: 'dGVzdA==' },
    };
    const result = await describeImages([mockImageBlock, secondImage], config, fallbackAuth);
    expect(result).toContain('图片 1');
    expect(result).toContain('HTTP 429');
    expect(result).toContain('**图片 2:**');
    expect(result).toContain('Success');
  });
});
