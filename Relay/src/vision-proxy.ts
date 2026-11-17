/**
 * Vision Proxy — auxiliary vision model for text-only primary models.
 *
 * When the primary model cannot handle images, this module calls an auxiliary
 * vision model through the Anthropic Messages API (/v1/messages, x-api-key
 * auth) to describe images as text, which is then forwarded to the primary
 * model. Any Anthropic-compatible endpoint can serve the vision model.
 *
 * Configuration priority (highest first):
 *   1. per-agent `visionModel` in relay.json
 *   2. global env `RELAY_VISION_MODEL`
 *
 * Credential priority (highest first):
 *   1. per-agent `visionModel.apiKey`
 *   2. global env `RELAY_VISION_API_KEY`
 *   3. primary provider credential (catalog provider or ANTHROPIC_*)
 */

import type { VisionModelConfig } from './types.js';

// --- Content block types (aligned with Anthropic Messages API) ---

export interface ImageContentBlock {
  type: 'image';
  source: { type: 'base64'; media_type: string; data: string };
}

export interface TextContentBlock {
  type: 'text';
  text: string;
}

export type ContentBlock = ImageContentBlock | TextContentBlock;

// --- Config resolution ---

const DEFAULT_SYSTEM_PROMPT =
  '请描述这张图片的内容。';

/**
 * Resolve effective vision model config from agent config + env.
 * Returns null if vision proxy is not configured.
 */
export function resolveVisionConfig(
  agentVisionModel?: VisionModelConfig,
): {
  model: string;
  baseUrl: string;
  apiKey: string;
  systemPrompt: string;
} | null {
  const envModel = process.env['RELAY_VISION_MODEL'];
  const model = agentVisionModel?.model ?? envModel;
  if (!model) return null;

  // baseUrl: agent > env > (caller resolves from provider)
  const baseUrl =
    agentVisionModel?.baseUrl
    ?? process.env['RELAY_VISION_BASE_URL']
    ?? '';  // Empty = caller should use primary provider URL

  // apiKey: agent > env > (caller resolves from provider)
  const apiKey =
    agentVisionModel?.apiKey
    ?? process.env['RELAY_VISION_API_KEY']
    ?? '';  // Empty = caller should use primary provider key

  const systemPrompt = agentVisionModel?.systemPrompt
    ?? process.env['RELAY_VISION_SYSTEM_PROMPT']
    ?? DEFAULT_SYSTEM_PROMPT;

  return { model, baseUrl, apiKey, systemPrompt };
}

// --- Image description ---

interface VisionApiResponse {
  content?: Array<{ type: 'text'; text: string }>;
  error?: { message?: string };
}

type VisionConfig = {
  model: string;
  baseUrl: string;
  apiKey: string;
  systemPrompt: string;
};

/**
 * Describe images by calling a vision model.
 * Returns a text description of all images combined, or a fallback placeholder on error.
 */
export async function describeImages(
  imageBlocks: ImageContentBlock[],
  config: VisionConfig,
  fallbackAuth: { baseUrl: string; apiKey: string },
): Promise<string> {
  const descriptions: string[] = [];
  const effectiveApiKey = config.apiKey || fallbackAuth.apiKey;

  for (let i = 0; i < imageBlocks.length; i++) {
    const block = imageBlocks[i];
    try {
      const text = await callAnthropicVision(block, config, effectiveApiKey, fallbackAuth);

      if (!text?.trim()) {
        descriptions.push(`[图片${imageBlocks.length > 1 ? ` ${i + 1}` : ''}内容无法识别]`);
      } else {
        descriptions.push(
          imageBlocks.length > 1
            ? `**图片 ${i + 1}:**\n${text.trim()}`
            : text.trim(),
        );
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      descriptions.push(`[图片${imageBlocks.length > 1 ? ` ${i + 1}` : ''}描述失败：${msg}]`);
    }
  }

  return descriptions.join('\n\n');
}

/**
 * Call vision model via Anthropic Messages API format.
 * Uses x-api-key auth and source.base64 image format.
 */
async function callAnthropicVision(
  block: ImageContentBlock,
  config: { model: string; baseUrl: string; systemPrompt: string },
  apiKey: string,
  fallbackAuth: { baseUrl: string },
): Promise<string> {
  const baseUrl = config.baseUrl || fallbackAuth.baseUrl;
  const url = `${baseUrl.replace(/\/$/, '')}/v1/messages`;

  // Use source.base64 format: Anthropic-compatible endpoints may silently
  // ignore image_url blocks.
  const prompt: unknown[] = [
    {
      type: 'image',
      source: {
        type: 'base64',
        media_type: block.source.media_type,
        data: block.source.data,
      },
    },
    { type: 'text', text: config.systemPrompt },
  ];

  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
    },
    signal: AbortSignal.timeout(30_000),
    body: JSON.stringify({
      model: config.model,
      max_tokens: 1024,
      messages: [{ role: 'user', content: prompt }],
    }),
  });

  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${body.slice(0, 200)}`);
  }

  const data = (await res.json()) as VisionApiResponse;
  if (data.error) {
    throw new Error(data.error.message ?? 'Unknown API error');
  }

  const text = data.content?.filter((b) => b.type === 'text').map((b) => b.text).join('\n');
  return text?.trim() ?? '';
}

// --- Content block helpers ---

/**
 * Extract image blocks from a StreamJsonMessage content array.
 */
export function extractImageBlocks(content: unknown[]): ImageContentBlock[] {
  return content.filter(
    (b): b is ImageContentBlock =>
      typeof b === 'object' && b !== null && (b as Record<string, unknown>).type === 'image',
  );
}

/**
 * Replace image blocks in a content array with a text description.
 * Returns a new content array with only text blocks.
 */
export function replaceImagesWithText(
  content: unknown[],
  description: string,
): TextContentBlock[] {
  const textBlocks: TextContentBlock[] = [];

  // Preserve existing text blocks
  for (const block of content) {
    if (typeof block === 'object' && block !== null && (block as Record<string, unknown>).type === 'text') {
      textBlocks.push({ type: 'text', text: (block as TextContentBlock).text });
    }
  }

  // Add image description — frame as user-sent visual content so the
  // primary text-only model treats the description as first-class input
  // instead of an opaque reference to something it "can't see".
  textBlocks.push({
    type: 'text',
    text: `[用户发送了一张图片，以下是图片的详细描述：]\n${description}\n[请根据以上图片描述内容来理解和回答用户的问题。]`,
  });

  return textBlocks;
}
