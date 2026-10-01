// Tests del front (web/app.js) sin navegador: se ejecuta en un contexto `vm` con un DOM mínimo falso.
//   docker run --rm -v "$PWD:/m" -w /m node:20-alpine node --test tests/js/
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const CODE = fs.readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8');

class El {
  constructor(tag) { this.nodeType = 1; this.tag = tag; this.children = []; this.style = {}; this.className = ''; this.textContent = ''; }
  setAttribute() {}
  addEventListener() {}
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = c; }
}
const textOf = (n) => (n == null ? '' : n.text ?? n.textContent + (n.children || []).map(textOf).join(' '));
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
const jwt = (payload) => `${b64({ alg: 'none' })}.${b64(payload)}.firma`;

const METRICS = {
  generated_at: '2026-09-29T14:00:00Z', replaying: false,
  flows: { running: 1, awaiting_hitl: 0, stalled: 0, finished: 0, by_status: {} },
  tokens: { input: 0, cached: 0, output: 0, thoughts: 0, total: 0 },
  cost: { currency: 'USD', total: null, avg_per_flow: null, priced_flows: 0, unpriced_flows: 0, unpriced_models: [], cached_at_input_rate: [] },
  avg: { duration_ms: null, tokens_per_flow: null }, by_agent: [], recent: [], archive: { enabled: false }, skipped_events: 0,
};

/** Carga app.js como lo haría el navegador y deja correr el primer ciclo. */
async function corre({ apiBase = '', path = '/', conToken = true, status = 200 } = {}) {
  const calls = [];
  const token = jwt({ email: 'a@b.cl', exp: Math.floor(Date.now() / 1000) + 3600 });
  const store = conToken ? { 'pm.id_token': token } : {};
  const app = new El('div');
  const location = {
    pathname: path, search: '', origin: 'https://monitor.example.com',
    assign: (u) => calls.push({ nav: u }), replace: (u) => calls.push({ nav: u }),
  };
  const ctx = vm.createContext({
    document: { getElementById: () => app, createElement: (t) => new El(t), createTextNode: (t) => ({ nodeType: 3, text: t }), addEventListener() {}, hidden: false },
    sessionStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } },
    location, history: {}, URLSearchParams, TextEncoder, atob, btoa, crypto: globalThis.crypto, console,
    setTimeout: () => 0, clearTimeout() {},
    fetch: async (url, opts) => {
      calls.push({ url, opts });
      if (String(url).endsWith('config.json')) {
        return { ok: true, status: 200, json: async () => ({ apiBase, cognitoDomain: 'login.example.com', clientId: 'c1', region: 'us-east-1' }) };
      }
      return { ok: status === 200, status, json: async () => METRICS };
    },
  });
  vm.runInContext(CODE, ctx);
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
  return { calls, app, token, store };
}
const metricsCall = (calls) => calls.find((c) => String(c.url || '').includes('/api/monitor/metrics'));

test('con apiBase (Amplify) llama al API Gateway y envía el token como Bearer', async () => {
  const { calls, token } = await corre({ apiBase: 'https://abc123.execute-api.us-east-1.amazonaws.com' });
  const c = metricsCall(calls);
  assert.equal(c.url, 'https://abc123.execute-api.us-east-1.amazonaws.com/api/monitor/metrics');
  assert.equal(c.opts.headers.Authorization, `Bearer ${token}`);
});

test('la barra final de apiBase no genera doble barra', async () => {
  const { calls } = await corre({ apiBase: 'https://abc123.execute-api.us-east-1.amazonaws.com/' });
  assert.equal(metricsCall(calls).url, 'https://abc123.execute-api.us-east-1.amazonaws.com/api/monitor/metrics');
});

test('con apiBase vacío (local) llama al mismo origen', async () => {
  const { calls } = await corre({ apiBase: '' });
  assert.equal(metricsCall(calls).url, '/api/monitor/metrics');
});

test('la configuración se pide una sola vez', async () => {
  const { calls } = await corre();
  assert.equal(calls.filter((c) => String(c.url).endsWith('config.json')).length, 1);
});

test('con datos válidos pinta el panel', async () => {
  const { app } = await corre();
  assert.match(textOf(app), /Monitor PRISMA/);
  assert.match(textOf(app), /Flujos corriendo/);
});

test('401 borra el token y manda al login', async () => {
  const { calls, store } = await corre({ status: 401 });
  assert.ok(calls.some((c) => c.nav === '/login'));
  assert.equal(store['pm.id_token'], undefined);
});

test('403 muestra acceso restringido y no redirige', async () => {
  const { calls, app } = await corre({ status: 403 });
  assert.match(textOf(app), /Acceso restringido/);
  assert.ok(!calls.some((c) => c.nav));
});

test('sin token en / redirige al login sin consultar métricas', async () => {
  const { calls } = await corre({ conToken: false });
  assert.ok(calls.some((c) => c.nav === '/login'));
  assert.equal(metricsCall(calls), undefined);
});

test('seguridad: el código (sin comentarios) no usa APIs que inyectan HTML', () => {
  const sinComentarios = CODE.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.doesNotMatch(sinComentarios, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/);
});
