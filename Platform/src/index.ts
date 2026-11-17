import { fileURLToPath } from 'node:url';
import { createHttpServer } from './http-server.js';

function readPort(): number {
  const raw = process.env['PLATFORM_HTTP_PORT'] ?? process.env['PORT'] ?? '8787';
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1 || n > 65535) {
    throw new Error(`Invalid PLATFORM_HTTP_PORT/PORT: ${raw}`);
  }
  return n;
}

export function startFromEnv(): ReturnType<typeof createHttpServer> {
  const port = readPort();
  const server = createHttpServer();
  server.listen(port, () => {
    console.log(JSON.stringify({
      ts: new Date().toISOString(),
      level: 'info',
      msg: 'platform http listening',
      port,
    }));
  });
  return server;
}

const isMain = process.argv[1] === fileURLToPath(import.meta.url);

if (isMain) {
  startFromEnv();
}
