/** Test-only bridge: unchanged frontend at :3000 + actual isolated runtime at :8091. */
import http from 'node:http';
if (!process.env.CELA_API_TOKEN) throw new Error('A server-side fixture token is required');
http.createServer((req, res) => {
  const api = req.url.startsWith('/api/backend/');
  const upstream = http.request({ hostname: '127.0.0.1', port: api ? 8091 : 3000,
    path: api ? req.url.replace('/api/backend', '') : req.url, method: req.method,
    headers: { ...req.headers, host: '127.0.0.1', ...(api ? { authorization: `Bearer ${process.env.CELA_API_TOKEN}` } : {}) },
  }, response => { res.writeHead(response.statusCode, response.headers); response.pipe(res); });
  upstream.on('error', () => { res.writeHead(502); res.end(); });
  req.pipe(upstream);
}).listen(3001, '0.0.0.0');
