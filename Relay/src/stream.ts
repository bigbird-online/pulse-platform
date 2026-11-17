import { createInterface } from 'node:readline';
import type { Readable, Writable } from 'node:stream';
import type { StreamJsonMessage } from './types.js';

/**
 * Write a StreamJsonMessage to stdin as JSON + newline (NDJSON).
 * Throws if the stream is not writable.
 */
export function writeMessage(stdin: Writable, message: StreamJsonMessage): void {
  if (stdin.destroyed) {
    throw new Error('stdin is destroyed, cannot write message');
  }
  const line = JSON.stringify(message) + '\n';
  stdin.write(line);
}

/** A stdout line is a result event if it declares `"type":"result"` near its head. */
function looksLikeResultLine(trimmed: string): boolean {
  if (!trimmed.startsWith('{')) return false;
  return /^\{\s*"type"\s*:\s*"result"/.test(trimmed.slice(0, 64));
}

/**
 * Best-effort recover the turn-completion fields from a result line that failed
 * strict JSON.parse. Only the fields finishTurn / channel handlers actually read
 * (type, subtype, result, model) are salvaged via narrow regex; everything else
 * is dropped. Returns a minimal valid result message so the turn closes cleanly.
 */
function salvageResultMessage(trimmed: string): StreamJsonMessage {
  const msg: StreamJsonMessage = { type: 'result' };
  const subtype = /"subtype"\s*:\s*"([^"]*)"/.exec(trimmed)?.[1];
  if (subtype) msg['subtype'] = subtype;
  const model = /"model"\s*:\s*"([^"]*)"/.exec(trimmed)?.[1];
  if (model) msg['model'] = model;
  // Salvaged turns have no reliably-parseable result text; leave it empty so the
  // streaming card simply closes on its already-accumulated content.
  msg['result'] = '';
  msg['salvaged'] = true;
  return msg;
}

/**
 * Create an NDJSON stream reader on stdout.
 * Each valid JSON line is passed to onMessage.
 * Invalid JSON lines trigger onError (stream continues).
 * When stdout closes, onClose is called (if provided).
 */
export function createStreamReader(
  stdout: Readable,
  onMessage: (msg: StreamJsonMessage) => void,
  onError: (err: Error) => void,
  onClose?: () => void,
): void {
  const rl = createInterface({ input: stdout });

  rl.on('line', (line: string) => {
    const trimmed = line.trim();
    if (trimmed === '') return;

    try {
      const parsed = JSON.parse(trimmed) as StreamJsonMessage;
      onMessage(parsed);
    } catch {
      // A malformed line must not silently strand turn-end bookkeeping. The
      // `result` event is the sole turn-completion signal for a persistent CC
      // session (no process exit between turns); if its line fails to parse,
      // downstream finishTurn / Feishu streaming-card close never fire and the
      // next turn's reply gets PUT into the previous (stale) card — surfacing
      // as "答非所问 / 卡片显示在新消息之前". So when a failed line still looks
      // like a result event, recover a minimal synthetic result and drive the
      // turn closed anyway, in addition to reporting the parse error.
      if (looksLikeResultLine(trimmed)) {
        onMessage(salvageResultMessage(trimmed));
      }
      onError(new Error(`Invalid NDJSON line: ${trimmed}`));
    }
  });

  if (onClose) {
    rl.on('close', onClose);
  }
}
