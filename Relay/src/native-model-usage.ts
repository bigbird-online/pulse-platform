/** Native producer reductions; the Host independently validates the partition. */
import {nativeObject, nativeLabel, nativeCounts, validateNativeModelUsage, usageKeys,
  type Counts, type ModelQuantity, type NativeModelUsage} from './collaboration-capture.js';
export {nativeObject, nativeLabel, nativeCounts, usageKeys, validateNativeModelUsage,
  type Counts, type ModelQuantity, type NativeModelUsage} from './collaboration-capture.js';

/** Reduce only this dispatch's completed native calls. No session cumulative
 * subtraction, startup baseline or RPC is needed. A gap (including compaction)
 * cannot become zero/complete; durable native history is the backfill source.
 * Repeated native timestamps are ambiguous, not two proven distinct calls.
 */
export function piNativeTurnUsage(messages: unknown[]): Counts | undefined {
  if (!messages.length || messages.length > 10000) return;
  const total: Counts = Object.fromEntries(usageKeys('pi').map(key => [key, 0]));
  const timestamps = new Set<number>();
  for (const item of messages) {
    const row = nativeObject(item), native = nativeObject(row.usage);
    if (row.timestamp !== undefined) {
      if (typeof row.timestamp !== 'number' || !Number.isSafeInteger(row.timestamp)
          || row.timestamp < 0 || timestamps.has(row.timestamp)) return;
      timestamps.add(row.timestamp);
    }
    const usage = nativeCounts({input_tokens: native.input, output_tokens: native.output,
      cache_read_input_tokens: native.cacheRead, cache_creation_input_tokens: native.cacheWrite}, 'pi');
    if (!usage) return;
    const callTotal = Object.values(usage).reduce((a, b) => a + b, 0);
    if (!Number.isSafeInteger(callTotal) || (native.totalTokens !== undefined && native.totalTokens !== callTotal)) return;
    for (const key of usageKeys('pi')) {
      total[key] = total[key]! + usage[key]!;
      if (!Number.isSafeInteger(total[key])) return;
    }
  }
  if (!Number.isSafeInteger(Object.values(total).reduce((a, b) => a + b, 0))) return;
  return total;
}

/** Inputs are real message_end allowlisted metadata, never assistant text. */
export function piNativeModelUsage(messages: unknown[], expected: unknown): NativeModelUsage | undefined {
  if (!messages.length || messages.length > 10000) return;
  const models: ModelQuantity[] = [];
  for (const item of messages) {
    const row = nativeObject(item), native = nativeObject(row.usage);
    const usage = nativeCounts({input_tokens: native.input, output_tokens: native.output,
      cache_read_input_tokens: native.cacheRead, cache_creation_input_tokens: native.cacheWrite}, 'pi');
    const model = nativeLabel(row.model), provider = nativeLabel(row.provider);
    if (!usage || !model || !provider) return;
    if (native.totalTokens !== undefined && native.totalTokens !== Object.values(usage).reduce((a,b) => a+b,0)) return;
    models.push({model, provider, usage});
  }
  return validateNativeModelUsage({schema: 'pulse_native_model_usage/v1', runtime: 'pi', models}, expected, 'pi');
}
