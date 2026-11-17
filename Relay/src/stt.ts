/**
 * Speech-to-text via Anthropic STT WebSocket (same service CC uses).
 * Auth: CLAUDE_CODE_OAUTH_TOKEN (already available in Relay env).
 * Protocol: wss://api.anthropic.com/api/ws/speech_to_text/voice_stream
 *   - Audio format: linear16, 16kHz, mono
 *   - Send audio chunks → send CloseStream → receive TranscriptEndpoint
 */

import WebSocket from 'ws';
import { spawn } from 'node:child_process';
import { relayChildEnv } from './relay-secrets.js';

const STT_PATH = '/api/ws/speech_to_text/voice_stream';
const KEEPALIVE_MSG = '{"type":"KeepAlive"}';
const CLOSE_STREAM_MSG = '{"type":"CloseStream"}';

/** Convert any audio buffer to linear16 PCM (16kHz mono) via ffmpeg. */
function toLinear16(input: Buffer): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const ffmpeg = spawn('ffmpeg', [
      '-i', 'pipe:0',
      '-f', 's16le',
      '-ar', '16000',
      '-ac', '1',
      'pipe:1',
    ], { stdio: ['pipe', 'pipe', 'pipe'], env: relayChildEnv() });

    const chunks: Buffer[] = [];
    ffmpeg.stdout.on('data', (chunk: Buffer) => chunks.push(chunk));
    ffmpeg.on('close', (code) => {
      if (code === 0) resolve(Buffer.concat(chunks));
      else reject(new Error(`ffmpeg exited with code ${code}`));
    });
    ffmpeg.on('error', reject);
    ffmpeg.stdin.write(input);
    ffmpeg.stdin.end();
  });
}

/**
 * Transcribe audio using Anthropic's STT WebSocket.
 * @param audioBuffer Raw audio bytes (any format ffmpeg supports)
 * @param oauthToken OAuth token for Anthropic API auth
 * @param language BCP-47 language code (default: "zh")
 */
export function transcribeAudio(
  audioBuffer: Buffer,
  oauthToken: string,
  language: string = 'zh',
): Promise<string> {
  return new Promise(async (resolve, reject) => {
    let pcm: Buffer;
    try {
      pcm = await toLinear16(audioBuffer);
    } catch (err) {
      reject(new Error(`Audio conversion failed: ${err}`));
      return;
    }

    if (pcm.length === 0) {
      reject(new Error('Audio conversion produced empty output'));
      return;
    }

    const baseUrl = (process.env['VOICE_STREAM_BASE_URL'] ??
      'wss://api.anthropic.com');

    const params = new URLSearchParams({
      encoding: 'linear16',
      sample_rate: '16000',
      channels: '1',
      endpointing_ms: '3000',
      utterance_end_ms: '5000',
      language,
      use_conversation_engine: 'true',
      stt_provider: 'deepgram-nova3',
    });

    const url = `${baseUrl}${STT_PATH}?${params}`;

    const ws = new WebSocket(url, {
      headers: {
        'Authorization': `Bearer ${oauthToken}`,
        'x-app': 'relay',
      },
    });

    let transcript = '';       // latest interim text for current utterance
    const finalized: string[] = [];  // completed utterances
    let settled = false;

    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        ws.close();
        reject(new Error('STT timeout (60s)'));
      }
    }, 60_000);

    const finish = (text: string): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      ws.close();
      resolve(text);
    };

    const fail = (err: string): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      ws.close();
      reject(new Error(err));
    };

    ws.on('open', () => {
      ws.send(KEEPALIVE_MSG);
      // Send PCM data in paced chunks simulating real-time audio
      // 16kHz × 2 bytes × 1 channel = 32000 bytes/sec
      const CHUNK_SIZE = 3200; // 100ms of audio
      const CHUNK_INTERVAL_MS = 100;
      let offset = 0;

      const sendNext = (): void => {
        if (settled || offset >= pcm.length) {
          // All audio sent — send CloseStream after a grace period
          if (!settled) {
            setTimeout(() => {
              if (!settled) ws.send(CLOSE_STREAM_MSG);
            }, 2000);
          }
          return;
        }
        const end = Math.min(offset + CHUNK_SIZE, pcm.length);
        ws.send(pcm.subarray(offset, end));
        offset = end;
        setTimeout(sendNext, CHUNK_INTERVAL_MS);
      };

      sendNext();
    });

    ws.on('message', (data: WebSocket.Data) => {
      try {
        const msg = JSON.parse(data.toString()) as {
          type: string;
          data?: string;
          description?: string;
          error_code?: string;
          message?: string;
        };

        switch (msg.type) {
          case 'TranscriptText':
            if (msg.data) transcript = msg.data;
            console.log({ ts: new Date().toISOString(), level: 'debug', module: 'stt', msg: 'TranscriptText', text: transcript });
            break;
          case 'TranscriptEndpoint':
            // Collect finalized utterance, don't resolve yet — more may follow
            console.log({ ts: new Date().toISOString(), level: 'debug', module: 'stt', msg: 'TranscriptEndpoint', utterance: transcript, totalSoFar: finalized.length });
            if (transcript) {
              finalized.push(transcript);
              transcript = '';
            }
            break;
          case 'TranscriptError':
            fail(`STT error: ${msg.description ?? msg.error_code ?? 'unknown'}`);
            break;
          case 'error':
            fail(`STT server error: ${msg.message ?? 'unknown'}`);
            break;
        }
      } catch {
        // ignore non-JSON messages
      }
    });

    ws.on('close', () => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        // Include any remaining interim text
        if (transcript) finalized.push(transcript);
        const full = finalized.join('');
        console.log({ ts: new Date().toISOString(), level: 'info', module: 'stt', msg: 'ws closed', segments: finalized.length, fullText: full });
        if (full) {
          resolve(full);
        } else {
          reject(new Error('STT returned empty transcript'));
        }
      }
    });

    ws.on('error', (err: Error) => {
      fail(`STT connection error: ${err.message}`);
    });
  });
}
