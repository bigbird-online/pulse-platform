/** The agy model catalog, free of runtime imports: launch selection and the agy runtime both read it. */
/**
 * `agy models`（1.5.13，wi_20261120_666 N01 fixture `00-models.txt`）的全部模型，按实测顺序。
 * 同一模型族的后缀（-low/-medium/-high/-xhigh）即思考档位：/model 选模型族，/think 选档位。
 */
export const AGY_MODELS: ReadonlyArray<{ model: string; displayName: string }> = [
  { model: 'gemini-4-neon-high', displayName: 'Gemini 4 Neon (High)' },
  { model: 'gemini-4-neon-xhigh', displayName: 'Gemini 4 Neon (XHigh)' },
  { model: 'gemini-4-neon-medium', displayName: 'Gemini 4 Neon (Medium)' },
  { model: 'gemini-4-neon-low', displayName: 'Gemini 4 Neon (Low)' },
  { model: 'gemini-4-argon-medium', displayName: 'Gemini 4 Argon (Medium)' },
  { model: 'gemini-4-argon-high', displayName: 'Gemini 4 Argon (High)' },
  { model: 'gemini-4-argon-low', displayName: 'Gemini 4 Argon (Low)' },
  { model: 'gemini-4-hydrogen-high', displayName: 'Gemini 4 Hydrogen (High)' },
  { model: 'gemini-4-hydrogen-xhigh', displayName: 'Gemini 4 Hydrogen (XHigh)' },
  { model: 'gemini-4-hydrogen-medium', displayName: 'Gemini 4 Hydrogen (Medium)' },
  { model: 'gemini-4-hydrogen-low', displayName: 'Gemini 4 Hydrogen (Low)' },
  { model: 'gemini-pro-orchestrator-neon', displayName: 'Orchestrator (Neon)' },
  { model: 'gemini-pro-orchestrator-g1-pro', displayName: 'Orchestrator (Argon)' },
  { model: 'gemini-3.8-flash-high', displayName: 'Gemini 3.8 Flash (High)' },
  { model: 'gemini-3.8-flash-medium', displayName: 'Gemini 3.8 Flash (Medium)' },
  { model: 'gemini-3.8-flash-low', displayName: 'Gemini 3.8 Flash (Low)' },
  { model: 'claude-opus-5-5-low', displayName: 'Claude Opus 5.5 (Low)' },
  { model: 'claude-opus-5-5-medium', displayName: 'Claude Opus 5.5 (Medium)' },
  { model: 'claude-opus-5-5-high', displayName: 'Claude Opus 5.5 (High)' },
  { model: 'claude-sonnet-5-5-low', displayName: 'Claude Sonnet 5.5 (Low)' },
  { model: 'claude-sonnet-5-5-medium', displayName: 'Claude Sonnet 5.5 (Medium)' },
  { model: 'claude-sonnet-5-5-high', displayName: 'Claude Sonnet 5.5 (High)' },
  { model: 'gpt-oss-120b-medium', displayName: 'GPT-OSS 120B (Medium)' },
];
export const DEFAULT_AGY_MODEL = 'gemini-4-neon-high';
export const AGY_MODEL_PROVIDER = 'agy';

export function isAgyModel(model: string | undefined): model is string {
  return !!model && AGY_MODELS.some((entry) => entry.model === model);
}
