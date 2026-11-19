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
  addEventListener(ev, fn) { (this.handlers ||= {})[ev] = fn; }
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

const ACTIVITY = {
  generated_at: '2026-11-19T14:00:00Z', window_s: 60, active_users: 7, requests_per_s: 2.5, requests_total: 1234,
  latency: { p50_ms: 42, p95_ms: 180 }, top_routes: [{ route: '/api/students', count: 30 }, { route: '/api/auth/me', count: 12 }],
  by_role: { TEACHER: 20, ADMIN: 3, SUPERADMIN: 1, none: 2 },
  recent: [
    { ts: '2026-11-19T14:00:00Z', user: 'a1b2c3d4e5f6', role: 'TEACHER', method: 'GET', route: '/api/students', status: 200, latency_ms: 35 },
    { ts: '2026-11-19T13:59:59Z', user: 'anon', role: 'none', method: 'POST', route: '/api/auth/login', status: 401, latency_ms: 12 },
  ],
  skipped_events: 0,
};
const isActivity = (url) => String(url).includes('/api/monitor/activity');

/** Carga app.js como lo haría el navegador y deja correr el primer ciclo. */
async function corre({ apiBase = '', path = '/', conToken = true, status = 200, activityStatus = status, activityBody = ACTIVITY } = {}) {
  const calls = [];
  const timers = new Map();
  let nextTimer = 1;
  let onVisibility = null;
  const token = jwt({ email: 'a@b.cl', exp: Math.floor(Date.now() / 1000) + 3600 });
  const store = conToken ? { 'pm.id_token': token } : {};
  const app = new El('div');
  const location = {
    pathname: path, search: '', origin: 'https://monitor.example.com',
    assign: (u) => calls.push({ nav: u }), replace: (u) => calls.push({ nav: u }),
  };
  const ctx = vm.createContext({
    document: { getElementById: () => app, createElement: (t) => new El(t), createTextNode: (t) => ({ nodeType: 3, text: t }), addEventListener: (ev, fn) => { if (ev === 'visibilitychange') onVisibility = fn; }, hidden: false },
    sessionStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } },
    location, history: {}, URLSearchParams, TextEncoder, atob, btoa, crypto: globalThis.crypto, console,
    setTimeout: (fn) => { const id = nextTimer++; timers.set(id, fn); return id; }, clearTimeout: (id) => { timers.delete(id); },
    fetch: async (url, opts) => {
      calls.push({ url, opts });
      if (String(url).endsWith('config.json')) {
        return { ok: true, status: 200, json: async () => ({ apiBase, cognitoDomain: 'login.example.com', clientId: 'c1', region: 'us-east-1' }) };
      }
      if (isActivity(url)) {
        if (activityStatus === 'throw') throw new Error('red caída');
        return { ok: activityStatus === 200, status: activityStatus, json: async () => activityBody };
      }
      return { ok: status === 200, status, json: async () => METRICS };
    },
  });
  vm.runInContext(CODE, ctx);
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
  return { calls, app, token, store, ctx, timers, visibility: () => onVisibility() };
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

// ── pestaña «Uso» ───────────────────────────────────────────────────────────
const walk = (n, out = []) => { if (n && n.nodeType === 1) { out.push(n); (n.children || []).forEach((c) => walk(c, out)); } return out; };
const byClass = (n, cls) => walk(n).filter((x) => String(x.className).split(' ').includes(cls));
const usage = async (m = ACTIVITY) => {
  const { ctx } = await corre();
  return vm.runInContext('renderUsage', ctx)(m, { stale: false });
};

test('renderUsage pinta tarjetas, tabla de rutas y log con sus columnas', async () => {
  const t = textOf(await usage());
  for (const s of ['Usuarios activos', 'Req/s', 'Latencia p50 / p95', '/api/students', 'Hora', 'Rol', 'Método', 'Ruta', 'Status', 'ms']) {
    assert.ok(t.includes(s), `falta «${s}»`);
  }
  assert.match(t, /42 \/ 180/);
});

test('cada rol usa su propia clase role-<ROL>', async () => {
  const n = await usage();
  const roles = walk(n).map((x) => String(x.className)).join(' ');
  for (const r of ['SUPERADMIN', 'ADMIN', 'TEACHER', 'none']) assert.ok(roles.includes(`role-${r}`), r);
  const log = byClass(n, 'role-TEACHER').filter((x) => x.tag === 'td' || x.tag === 'span');
  assert.ok(log.length >= 1);
});

test('latencia null se muestra como «—»', async () => {
  const n = await usage({ ...ACTIVITY, latency: { p50_ms: null, p95_ms: null } });
  const card = walk(n).find((x) => x.className === 'card' && textOf(x).includes('Latencia'));
  assert.ok(textOf(card).includes('—'));
  assert.ok(!/null|NaN/.test(textOf(card)));
});

test('log vacío muestra el texto vacío', async () => {
  const t = textOf(await usage({ ...ACTIVITY, recent: [], top_routes: [], by_role: {} }));
  assert.match(t, /Aún no hay actividad/);
});

test('el texto del evento se inserta como texto: una ruta con HTML no crea elementos', async () => {
  const evil = '<img onerror=x>';
  const n = await usage({ ...ACTIVITY, recent: [{ ...ACTIVITY.recent[0], route: evil }] });
  assert.ok(textOf(n).includes(evil));
  assert.ok(!walk(n).some((x) => x.tag === 'img'));
});

test('la pestaña por defecto es agéntico y el poll pide ambos endpoints', async () => {
  const { calls, app } = await corre();
  assert.ok(metricsCall(calls) && calls.some((c) => isActivity(c.url)));
  assert.match(textOf(app), /Flujos corriendo/);
  assert.ok(!/Usuarios activos/.test(textOf(app)));
});

test('clic en la pestaña «Uso» muestra la sección de uso', async () => {
  const { app } = await corre();
  const tab = walk(app).find((x) => x.tag === 'button' && textOf(x) === 'Uso');
  assert.ok(tab, 'no hay pestaña Uso');
  tab.handlers.click();
  assert.match(textOf(app), /Usuarios activos/);
});

test('si falla actividad pero métricas responde, no se vacía la pantalla', async () => {
  const { app } = await corre({ activityStatus: 500 });
  assert.match(textOf(app), /Flujos corriendo/);
});

test('si falla la red de actividad pero métricas responde, tampoco', async () => {
  const { app } = await corre({ activityStatus: 'throw' });
  assert.match(textOf(app), /Flujos corriendo/);
});

test('401 solo en actividad también manda al login', async () => {
  const { calls, store } = await corre({ status: 200, activityStatus: 401 });
  assert.ok(calls.some((c) => c.nav === '/login'));
  assert.equal(store['pm.id_token'], undefined);
});

test('403 solo en actividad muestra acceso restringido', async () => {
  const { app } = await corre({ status: 200, activityStatus: 403 });
  assert.match(textOf(app), /Acceso restringido/);
});

const drain = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };

test('volver a la ventana conserva la pestaña elegida y los datos', async () => {
  const { app, visibility } = await corre();
  walk(app).find((x) => x.tag === 'button' && textOf(x) === 'Uso').handlers.click();
  visibility();
  await drain();
  assert.match(textOf(app), /Usuarios activos/);
});

test('dos polls solapados no dejan dos cadenas de temporizadores vivas', async () => {
  const { timers, visibility } = await corre();
  assert.equal(timers.size, 1);
  visibility(); visibility();
  await drain();
  assert.equal(timers.size, 1);
});
