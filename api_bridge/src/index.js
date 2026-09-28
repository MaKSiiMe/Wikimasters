import { createClient } from 'redis';

import { buildApp } from './app.js';
import { loadConfig } from './config.js';

const config = loadConfig();

const redis = createClient({
  url: config.redisUrl,
  socket: { reconnectStrategy: (retries) => Math.min(retries * 200, 5000) },
});

const app = await buildApp({ config, redis });
redis.on('error', (err) => app.log.warn({ err: err.message }, 'redis error'));
await redis.connect();

let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  app.log.info({ signal }, 'shutting down');
  try {
    await app.close();
    await redis.quit();
  } finally {
    process.exit(0);
  }
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

await app.listen({ host: config.host, port: config.port });
app.log.info({ corsOrigins: config.corsOrigins, mlApiUrl: config.mlApiUrl }, 'WikiMasters API bridge ready');
