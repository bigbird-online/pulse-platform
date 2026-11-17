/**
 * Web Terminal — browser-based CC session takeover.
 *
 * When a user sends /cc, a one-time token is created. The browser opens
 * /terminal/<token>, which serves an xterm.js page. A WebSocket connection
 * (/terminal/<token>/ws) bridges browser I/O to a node-pty CC process.
 *
 * The PTY is spawned only when the WebSocket connects, and killed when it
 * disconnects or the token expires (10 min inactivity).
 */

import { relayChildEnv, stripRelaySecrets } from './relay-secrets.js';
import { spawn, type IPty } from '@homebridge/node-pty-prebuilt-multiarch';
import { execSync } from 'node:child_process';
import { WebSocket } from 'ws';
import { randomUUID } from 'node:crypto';

export interface TerminalAgentInfo {
  tenantId: string;
  workspace: string;
  sessionId: string;
  linuxUser?: string;
  userHome?: string;
  /** Extra CC CLI args (e.g. ['--model', 'sonnet', '--settings', '{...}']). */
  ccArgs?: string[];
  /** Env vars to inject for CC auth (provider-specific). */
  ccEnv?: Record<string, string>;
}

interface TerminalSession extends TerminalAgentInfo {
  token: string;
  expiresAt: number;
  pty?: IPty;
  ws?: WebSocket;
  cleanupTimer?: ReturnType<typeof setTimeout>;
}

const TTL_MS = 10 * 60 * 1000; // 10 minutes

export class TerminalManager {
  private sessions = new Map<string, TerminalSession>();

  /** Create a one-time token for terminal access. Does NOT start the PTY yet. */
  createToken(agent: TerminalAgentInfo): string {
    const token = randomUUID();
    this.sessions.set(token, {
      ...agent,
      token,
      expiresAt: Date.now() + TTL_MS,
    });
    return token;
  }

  /** Validate token for HTTP GET (page load). Returns null if invalid/expired. */
  validateToken(token: string): boolean {
    const session = this.sessions.get(token);
    if (!session) return false;
    if (Date.now() > session.expiresAt) {
      this.cleanup(token);
      return false;
    }
    return true;
  }

  /**
   * Attach a WebSocket to a terminal session. Spawns the PTY if not already
   * running, then bridges WS ↔ PTY I/O.
   */
  attachWebSocket(token: string, ws: WebSocket): void {
    const session = this.sessions.get(token);
    if (!session) {
      ws.close(4004, 'Invalid or expired token');
      return;
    }
    if (session.ws) {
      ws.close(4009, 'Terminal already in use');
      return;
    }

    // Refresh expiry on connect
    session.expiresAt = Date.now() + TTL_MS;
    session.ws = ws;
    this.resetCleanupTimer(token);

    // Spawn PTY (wrapped in try-catch — posix_spawnp failure must not crash the relay)
    try {
      const isLinux = process.platform === 'linux';
      const claudeBin = process.env['CLAUDE_BIN'] ?? 'claude';

      // Build env injection for sudo -u path.
      // Priority: session.ccEnv (agent-specific) > process.env globals.
      const AUTH_PREFIXES = ['ANTHROPIC_'];
      const globalAuthEnv: Record<string, string> = {};
      for (const [key, value] of Object.entries(process.env)) {
        if (typeof value === 'string' && AUTH_PREFIXES.some(p => key.startsWith(p))) {
          globalAuthEnv[key] = value;
        }
      }
      // Agent-specific env takes precedence over globals
      const mergedEnv = { ...globalAuthEnv, ...(session.ccEnv ?? {}) };
      const envArgs = Object.entries(mergedEnv).map(([k, v]) => `${k}=${v}`);
      const ccArgs = session.ccArgs ?? [];

      if (isLinux && session.linuxUser) {
        // Server: sudo -u <linuxUser> env KEY=VAL ... claude --resume <sid> [ccArgs]
        session.pty = spawn('sudo', [
          '-u', session.linuxUser,
          'env',
          `HOME=${session.workspace}`,
          'PATH=/usr/local/bin:/usr/bin:/bin',
          ...envArgs,
          claudeBin, '--resume', session.sessionId,
          ...ccArgs,
        ], {
          cwd: session.workspace,
          env: stripRelaySecrets({ ...process.env as Record<string, string> }),
          cols: 80,
          rows: 24,
        });
      } else {
        // Local (macOS): resolve claude to absolute path (node-pty uses posix_spawnp)
        let claudeAbs = claudeBin;
        if (!claudeAbs.startsWith('/')) {
          try {
            claudeAbs = execSync(`which ${claudeBin}`, { encoding: 'utf-8', env: relayChildEnv() }).trim();
          } catch { /* fallback to bare name */ }
        }
        const env = stripRelaySecrets({ ...process.env as Record<string, string> });
        if (session.userHome) env['HOME'] = session.userHome;
        // Agent-specific env overrides
        for (const [k, v] of Object.entries(mergedEnv)) env[k] = v;
        session.pty = spawn(claudeAbs, ['--resume', session.sessionId, ...ccArgs], {
          cwd: session.workspace,
          env,
          cols: 80,
          rows: 24,
        });
      }

      // PTY → WS
      session.pty.onData((data: string) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'data', data }));
        }
      });

      // PTY exit → cleanup
      session.pty.onExit(({ exitCode }) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'exit', code: exitCode }));
          ws.close(1000, 'Process exited');
        }
        this.cleanup(token);
      });
    } catch (err) {
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        msg: 'Web Terminal PTY spawn failed',
        error: String(err),
      });
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'exit', code: -1 }));
        ws.close(1011, 'PTY spawn failed');
      }
      this.cleanup(token);
      return;
    }

    // WS → PTY
    ws.on('message', (raw: Buffer | string) => {
      const msg = typeof raw === 'string' ? raw : raw.toString();
      this.refreshExpiry(token);
      try {
        const parsed = JSON.parse(msg) as { type: string; data?: string; cols?: number; rows?: number };
        if (parsed.type === 'data' && parsed.data !== undefined) {
          session.pty?.write(parsed.data);
        } else if (parsed.type === 'resize' && parsed.cols && parsed.rows) {
          session.pty?.resize(parsed.cols, parsed.rows);
        }
      } catch {
        // Non-JSON message — treat as raw input
        session.pty?.write(msg);
      }
    });

    ws.on('close', () => {
      this.cleanup(token);
    });

    ws.on('error', () => {
      this.cleanup(token);
    });
  }

  private refreshExpiry(token: string): void {
    const session = this.sessions.get(token);
    if (!session) return;
    session.expiresAt = Date.now() + TTL_MS;
    this.resetCleanupTimer(token);
  }

  private resetCleanupTimer(token: string): void {
    const session = this.sessions.get(token);
    if (!session) return;
    if (session.cleanupTimer) clearTimeout(session.cleanupTimer);
    session.cleanupTimer = setTimeout(() => {
      if (session.ws?.readyState === WebSocket.OPEN) {
        session.ws.send(JSON.stringify({ type: 'exit', code: -1 }));
        session.ws.close(1000, 'Session timeout');
      }
      this.cleanup(token);
    }, TTL_MS);
  }

  /** Kill PTY, close WS, delete session. */
  cleanup(token: string): void {
    const session = this.sessions.get(token);
    if (!session) return;
    if (session.cleanupTimer) clearTimeout(session.cleanupTimer);
    try { session.pty?.kill(); } catch { /* already exited */ }
    try { session.ws?.close(); } catch { /* already closed */ }
    this.sessions.delete(token);
  }

  /** Clean up all sessions (on shutdown). */
  cleanupAll(): void {
    for (const token of this.sessions.keys()) {
      this.cleanup(token);
    }
  }

  /** Number of active sessions (for health check). */
  get size(): number {
    return this.sessions.size;
  }
}

/**
 * Static HTML page with xterm.js frontend.
 * Served at GET /terminal/:token.
 */
export function renderTerminalHtml(token: string): string {
  const wsUrl = `/terminal/${token}/ws`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PULSE Terminal</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { height: 100%; background: #1e1e1e; overflow: hidden; }
    #terminal { height: 100%; padding: 4px; }
    #status { position: fixed; top: 8px; right: 12px; font: 12px monospace; color: #888; z-index: 10; }
  </style>
</head>
<body>
  <div id="status"></div>
  <div id="terminal"></div>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js"></script>
  <script>
    const term = new Terminal({ fontSize: 14, cursorBlink: true });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById('terminal'));
    fit.fit();

    const status = document.getElementById('status');
    let ws;

    function connect() {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(proto + '//' + location.host + '${wsUrl}');

      ws.onopen = () => {
        status.textContent = 'connected';
        status.style.color = '#4CAF50';
        sendResize();
      };

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'data') term.write(msg.data);
        else if (msg.type === 'exit') {
          status.textContent = 'process exited (' + msg.code + ')';
          status.style.color = '#f44336';
        }
      };

      ws.onclose = (event) => {
        status.textContent = 'disconnected';
        status.style.color = '#f44336';
      };

      ws.onerror = () => { status.textContent = 'error'; status.style.color = '#f44336'; };
    }

    function sendResize() {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
      }
    }

    term.onData((data) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'data', data }));
      }
    });

    window.addEventListener('resize', () => { fit.fit(); sendResize(); });

    connect();
  </script>
</body>
</html>`;
}
