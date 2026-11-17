import { describe, it, expect, vi } from 'vitest';
import { PassThrough } from 'node:stream';
import { writeMessage, createStreamReader } from '../src/stream.js';

describe('writeMessage', () => {
  it('serializes message as JSON + newline', () => {
    const stdin = new PassThrough();
    const chunks: Buffer[] = [];
    stdin.on('data', (chunk: Buffer) => chunks.push(chunk));

    writeMessage(stdin, { type: 'human', content: 'hello' });

    const output = Buffer.concat(chunks).toString();
    expect(output).toBe('{"type":"human","content":"hello"}\n');
  });

  it('handles message with extra fields', () => {
    const stdin = new PassThrough();
    const chunks: Buffer[] = [];
    stdin.on('data', (chunk: Buffer) => chunks.push(chunk));

    writeMessage(stdin, { type: 'result', total_cost_usd: 0.05 });

    const parsed = JSON.parse(Buffer.concat(chunks).toString().trim());
    expect(parsed.type).toBe('result');
    expect(parsed.total_cost_usd).toBe(0.05);
  });

  it('throws when stdin is destroyed', () => {
    const stdin = new PassThrough();
    stdin.destroy();

    expect(() => writeMessage(stdin, { type: 'human', content: 'hi' }))
      .toThrow();
  });
});

describe('createStreamReader', () => {
  it('parses valid NDJSON lines', async () => {
    const stdout = new PassThrough();
    const messages: unknown[] = [];

    createStreamReader(
      stdout,
      (msg) => messages.push(msg),
      () => {},
    );

    stdout.write('{"type":"assistant","content":"hi"}\n');
    stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    stdout.end();

    // Wait for readline to process
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(messages).toHaveLength(2);
    expect(messages[0]).toEqual({ type: 'assistant', content: 'hi' });
    expect(messages[1]).toEqual({ type: 'result', total_cost_usd: 0.01 });
  });

  it('calls onError for invalid JSON without stopping', async () => {
    const stdout = new PassThrough();
    const messages: unknown[] = [];
    const errors: Error[] = [];

    createStreamReader(
      stdout,
      (msg) => messages.push(msg),
      (err) => errors.push(err),
    );

    stdout.write('not valid json\n');
    stdout.write('{"type":"assistant","content":"ok"}\n');
    stdout.end();

    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(errors).toHaveLength(1);
    expect(errors[0].message).toContain('not valid json');
    expect(messages).toHaveLength(1);
    expect(messages[0]).toEqual({ type: 'assistant', content: 'ok' });
  });

  it('skips empty lines', async () => {
    const stdout = new PassThrough();
    const messages: unknown[] = [];

    createStreamReader(
      stdout,
      (msg) => messages.push(msg),
      () => {},
    );

    stdout.write('\n');
    stdout.write('  \n');
    stdout.write('{"type":"assistant","content":"x"}\n');
    stdout.end();

    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(messages).toHaveLength(1);
  });

  it('salvages a malformed result line so turn-end is never stranded', async () => {
    const stdout = new PassThrough();
    const messages: Array<{ type: string; [k: string]: unknown }> = [];
    const errors: Error[] = [];

    createStreamReader(
      stdout,
      (msg) => messages.push(msg as { type: string }),
      (err) => errors.push(err),
    );

    // A result line that fails strict JSON.parse (unterminated string), but
    // clearly declares type:result. Must still drive a synthetic turn-end.
    stdout.write('{"type":"result","subtype":"success","model":"claude-opus-4-7","result":"oops\n');
    stdout.end();

    await new Promise((resolve) => setTimeout(resolve, 50));

    // Both: the original parse error is reported AND a salvaged result is emitted.
    expect(errors).toHaveLength(1);
    const resultMsgs = messages.filter((m) => m.type === 'result');
    expect(resultMsgs).toHaveLength(1);
    expect(resultMsgs[0]).toMatchObject({ type: 'result', subtype: 'success', model: 'claude-opus-4-7', salvaged: true });
  });

  it('does not salvage a non-result malformed line', async () => {
    const stdout = new PassThrough();
    const messages: unknown[] = [];
    const errors: Error[] = [];

    createStreamReader(
      stdout,
      (msg) => messages.push(msg),
      (err) => errors.push(err),
    );

    stdout.write('{"type":"assistant","content":"broken\n');
    stdout.end();

    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(errors).toHaveLength(1);
    expect(messages).toHaveLength(0);
  });

  it('calls onClose when stdout closes', async () => {
    const stdout = new PassThrough();
    const onClose = vi.fn();

    createStreamReader(stdout, () => {}, () => {}, onClose);

    stdout.end();

    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(onClose).toHaveBeenCalledOnce();
  });
});
