/**
 * Provider routing variables a CC child must not inherit from the host
 * process. A route is applied only when the caller passes it explicitly.
 */
export const INHERITED_ROUTING_ENV_KEYS = [
  'ANTHROPIC_BASE_URL',
  'ANTHROPIC_AUTH_TOKEN',
  'ANTHROPIC_API_KEY',
  'ANTHROPIC_DEFAULT_HAIKU_MODEL',
  'ANTHROPIC_DEFAULT_SONNET_MODEL',
  'ANTHROPIC_DEFAULT_OPUS_MODEL',
  'ANTHROPIC_CUSTOM_MODEL_OPTION',
] as const;

/** Credential prefixes of providers that are no longer routable. */
export const RETIRED_PROVIDER_ENV_PREFIXES = ['GLM_', 'KIMI_', 'MINIMAX_', 'DEEPSEEK_', 'XFYUN_'] as const;

export function stripInheritedProviderRoutingEnv(env: Record<string, string | undefined>): void {
  for (const key of INHERITED_ROUTING_ENV_KEYS) delete env[key];
  for (const key of Object.keys(env)) {
    if (RETIRED_PROVIDER_ENV_PREFIXES.some((prefix) => key.startsWith(prefix))) delete env[key];
  }
}
