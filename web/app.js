'use strict';
/*
 * Front del monitor PRISMA. Sin build ni dependencias.
 * - Login: Cognito Hosted UI con PKCE (código propio; no comparte nada con prisma-front).
 * - Datos: polling a /api/monitor/metrics y /api/monitor/activity (en paralelo) cada 3 s con el ID token como Bearer.
 * - Seguridad: todo se pinta con textContent/createElement (nunca innerHTML). La CSP solo permite
 *   el propio origen y el dominio de Cognito.
 * El servidor es quien exige SUPERADMIN; este front solo muestra el 401/403 que reciba.
 */

const POLL_MS = 3000;
const STORE = { token: 'pm.id_token', verifier: 'pm.verifier', state: 'pm.state', notice: 'pm.notice' };
const app = document.getElementById('app');

// ── utilidades ──────────────────────────────────────────────────────────────
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c.nodeType ? c : document.createTextNode(String(c)));
  return node;
}
const int = (n) => (n ?? 0).toLocaleString('es-CL');
const usd = (n) => (n == null ? null : '$' + n.toLocaleString('es-CL', { minimumFractionDigits: 4, maximumFractionDigits: 4 }));
function dur(ms) {
  if (ms == null) return '—';
  const s = Math.round(ms / 1000);
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
    : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
const hhmmss = (iso) => new Date(iso).toLocaleTimeString('es-CL', { hour12: false });
const session = {
  get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* modo privado */ } },
  del: (k) => { try { sessionStorage.removeItem(k); } catch { /* */ } },
};

// ── PKCE ────────────────────────────────────────────────────────────────────
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const randomString = (bytes = 48) => b64url(crypto.getRandomValues(new Uint8Array(bytes)));
const sha256 = async (text) => b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));

/*
 * /config.json trae los datos públicos de Cognito y `apiBase`, la dirección de la API:
 *   - local (el monitor sirve este front): apiBase = "" -> se llama al mismo origen.
 *   - Amplify: el build genera config.json con la URL del API Gateway, p. ej. "https://xxxx.execute-api...".
 * Se pide una sola vez; si falla, se reintenta en la siguiente consulta.
 */
let configPromise = null;
function loadConfig() {
  if (!configPromise) {
    configPromise = fetch('/config.json', { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error('No se pudo leer la configuración del monitor');
        return r.json();
      })
      .catch((e) => { configPromise = null; throw e; });
  }
  return configPromise;
}
const apiUrl = (cfg, path) => `${String(cfg.apiBase || '').replace(/\/+$/, '')}${path}`;

async function startLogin(provider) {
  const cfg = await loadConfig();
  const verifier = randomString(), state = randomString(16);
  session.set(STORE.verifier, verifier);
  session.set(STORE.state, state);
  const params = new URLSearchParams({
    response_type: 'code', client_id: cfg.clientId, redirect_uri: `${location.origin}/auth/callback`,
    scope: 'openid email profile', state, code_challenge: await sha256(verifier), code_challenge_method: 'S256',
  });
  if (provider) params.set('identity_provider', provider);
  location.assign(`https://${cfg.cognitoDomain}/oauth2/authorize?${params}`);
}

async function handleCallback() {
  const q = new URLSearchParams(location.search);
  const fail = (msg) => { session.set(STORE.notice, msg); location.replace('/login'); };
  if (q.get('error')) return fail(`Cognito rechazó el inicio de sesión: ${q.get('error_description') || q.get('error')}`);
  if (!q.get('code') || q.get('state') !== session.get(STORE.state)) return fail('El inicio de sesión no es válido o expiró. Intenta de nuevo.');
  const cfg = await loadConfig();
  const r = await fetch(`https://${cfg.cognitoDomain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code', client_id: cfg.clientId, code: q.get('code'),
      redirect_uri: `${location.origin}/auth/callback`, code_verifier: session.get(STORE.verifier) || '',
    }),
  });
  if (!r.ok) return fail('No se pudo completar el inicio de sesión con Cognito.');
  const { id_token: idToken } = await r.json();
  session.del(STORE.verifier); session.del(STORE.state);
  session.set(STORE.token, idToken);
  location.replace('/');
}

function claims() {
  const t = session.get(STORE.token);
  if (!t) return null;
  try {
    const payload = JSON.parse(atob(t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    return payload.exp * 1000 > Date.now() ? payload : null;
  } catch { return null; }
}

async function logout() {
  session.del(STORE.token);
  try {
    const cfg = await loadConfig();
    location.assign(`https://${cfg.cognitoDomain}/logout?` + new URLSearchParams({ client_id: cfg.clientId, logout_uri: `${location.origin}/login` }));
  } catch { location.assign('/login'); }
}

// ── pantallas ───────────────────────────────────────────────────────────────
function screen(title, message, ...actions) {
  const notice = session.get(STORE.notice);
  session.del(STORE.notice);
  app.replaceChildren(el('div', { class: 'center' },
    el('div', { class: 'login' },
      el('h1', { text: title }), el('p', { text: message }),
      notice ? el('div', { class: 'note', text: notice, role: 'alert' }) : null,
      el('div', { class: 'actions' }, ...actions))));
}

function renderLogin() {
  screen('Monitor PRISMA', 'Acceso restringido a SUPERADMIN.',
    el('button', { class: 'primary', onclick: () => startLogin('Google'), text: 'Continuar con Google' }),
    el('a', { onclick: () => startLogin(null), text: 'Otras opciones de inicio de sesión' }));
}

function renderForbidden() {
  screen('Acceso restringido', 'Tu cuenta no tiene el rol SUPERADMIN. Pide acceso a un administrador.',
    el('button', { onclick: logout, text: 'Cerrar sesión' }));
}

// ── dashboard ───────────────────────────────────────────────────────────────
const STATE = {
  running: ['Corriendo', 's-running'], awaiting_hitl: ['Esperando docente', 's-awaiting_hitl'], stalled: ['Detenido', 's-stalled'],
};
const FINAL = {
  success: ['Completado', 's-success'], degraded: ['Degradado', 's-awaiting_hitl'], compliance_blocked: ['Bloqueado por normativa', 's-bad'],
  hitl_rejected: ['Rechazado por docente', 's-bad'], cancelled: ['Cancelado', 's-stalled'], error: ['Error', 's-bad'],
};
const stateLabel = (row) => (row.state === 'finished' ? FINAL[row.status] || [row.status || 'Terminado', ''] : STATE[row.state] || [row.state, '']);

function costText(m) {
  const v = usd(m.cost.avg_per_flow);
  if (v == null) return m.cost.unpriced_models.length ? 'sin tarifa' : '—';
  return (m.cost.cached_at_input_rate.length ? '≈ ' : '') + v;
}

function card(title, big, sub, extra) {
  return el('div', { class: 'card' }, el('h2', { text: title }), el('div', { class: 'big num', text: big }), sub, extra);
}

const ROLES = ['SUPERADMIN', 'ADMIN', 'TEACHER', 'none'];
const roleCls = (r) => `role-${ROLES.includes(r) ? r : 'none'}`;
const ms = (n) => (n == null ? '—' : int(Math.round(n)));
const rate = (n) => (n == null ? '—' : n.toLocaleString('es-CL', { minimumFractionDigits: 1, maximumFractionDigits: 1 }));

/** Contenido de la pestaña «Uso» (actividad HTTP del BFF). Todo el texto va con textContent. */
function renderUsage(m, ctx) {
  const lat = m.latency || {};
  const kpis = el('div', { class: 'grid' },
    card('Usuarios activos', int(m.active_users), el('div', { class: 'sub', text: 'Últimos 60 s' })),
    card('Req/s', rate(m.requests_per_s), el('div', { class: 'sub' }, 'Últimos 60 s · total desde el inicio del monitor: ', el('b', { text: int(m.requests_total) }))),
    card('Latencia p50 / p95', lat.p50_ms == null && lat.p95_ms == null ? '—' : `${ms(lat.p50_ms)} / ${ms(lat.p95_ms)} ms`,
      el('div', { class: 'sub', text: 'Últimos 5 min' })),
    card('Requests por rol', int(Object.values(m.by_role || {}).reduce((a, b) => a + b, 0)),
      el('div', { class: 'sub', text: 'Últimos 5 min' }),
      el('div', { class: 'chips' }, Object.entries(m.by_role || {}).map(([r, n]) =>
        el('span', { class: `chip ${roleCls(r)}`, text: `${r}: ${int(n)}` })))),
  );

  const routesList = m.top_routes || [];
  const maxRoute = Math.max(1, ...routesList.map((r) => r.count));
  const routes = el('div', { class: 'section' }, el('h2', { text: 'Rutas más usadas (últimos 5 min)' }),
    routesList.length ? el('div', { class: 'bars' }, routesList.map((r) => {
      const fill = el('div', { class: 'bar-fill' });
      fill.style.width = `${Math.max(2, (r.count / maxRoute) * 100)}%`;
      return el('div', { class: 'bar-row' }, el('span', { text: r.route }), el('div', { class: 'bar-track' }, fill),
        el('span', { class: 'bar-val num', text: int(r.count) }));
    })) : el('div', { class: 'empty', text: 'Aún no hay rutas registradas.' }));

  const head = ['Hora', 'Rol', 'Método', 'Ruta', 'Status', 'ms'];
  const recent = m.recent || [];
  const log = el('div', { class: 'section' }, el('h2', { text: 'Actividad reciente' }),
    recent.length ? el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, head.map((h, i) => el('th', { class: i === 5 ? 'r' : '', text: h })))),
      el('tbody', {}, recent.map((r) => el('tr', {},
        el('td', { class: 'num', text: hhmmss(r.ts) }),
        el('td', {}, el('span', { class: `role ${roleCls(r.role)}`, text: r.role })),
        el('td', { text: r.method }), el('td', { text: r.route }),
        el('td', { class: `num ${r.status >= 500 ? 's-bad' : r.status >= 400 ? 's-awaiting_hitl' : ''}`, text: String(r.status) }),
        el('td', { class: 'r num', text: ms(r.latency_ms) })))))
    ) : el('div', { class: 'empty', text: 'Aún no hay actividad. Usa PRISMA (o lanza la carga de k6) para verla aquí.' }));

  const foot = m.skipped_events ? el('div', { class: 'foot', text: `${int(m.skipped_events)} eventos omitidos por no cumplir el contrato` }) : null;
  return el('div', {}, kpis, routes, log, foot);
}

function render(m, ctx) {
  const who = claims();
  const tab = ctx.tab || 'agentico';
  const tabs = el('div', { class: 'tabs', role: 'tablist' }, [['agentico', 'Agéntico'], ['uso', 'Uso']].map(([k, label]) =>
    el('button', { class: `tab${tab === k ? ' active' : ''}`, role: 'tab', 'aria-selected': tab === k ? 'true' : 'false', text: label, onclick: () => ctx.onTab && ctx.onTab(k) })));
  const stamp = (tab === 'uso' ? ctx.activity : m)?.generated_at;
  const header = el('div', { class: 'top' }, el('h1', { text: 'Monitor PRISMA' }), tabs,
    el('span', { class: 'live' }, el('span', { class: `dot${ctx.stale ? ' off' : ''}` }), ctx.stale ? 'sin conexión' : (stamp ? `actualizado ${hhmmss(stamp)}` : '')),
    el('div', { class: 'spacer' }), el('span', { class: 'who', text: who?.email || '' }),
    el('button', { onclick: logout, text: 'Cerrar sesión' }));

  let body;
  if (tab === 'uso') {
    body = ctx.activity ? renderUsage(ctx.activity, ctx) : el('div', { class: 'empty', text: 'No se pudo obtener la actividad todavía.' });
  } else if (m) body = renderAgentic(m);
  else body = el('div', { class: 'empty', text: 'No se pudieron obtener las métricas todavía.' });

  app.replaceChildren(el('div', { class: 'wrap' }, header,
    tab === 'agentico' && m?.replaying ? el('div', { class: 'banner', role: 'status', text: 'Reconstruyendo el historial desde Kafka… las cifras se completan en unos segundos.' }) : null,
    body));
}

function renderAgentic(m) {
  const f = m.flows, t = m.tokens;
  const kpis = el('div', { class: 'grid' },
    card('Flujos corriendo', int(f.running),
      el('div', { class: 'sub' }, 'Esperando docente: ', el('b', { text: int(f.awaiting_hitl) }), ' · Detenidos: ', el('b', { text: int(f.stalled) }))),
    card('Tokens usados', int(t.total),
      el('div', { class: 'sub' }, 'Entrada ', el('b', { text: int(t.input) }), ' (caché ', int(t.cached), ') · Salida ', el('b', { text: int(t.output) }), ' · Razonamiento ', int(t.thoughts))),
    card('Costo promedio por flujo', costText(m),
      el('div', { class: 'sub', title: m.cost.unpriced_models.join(', ') },
        m.cost.unpriced_models.length ? `Sin tarifa para: ${m.cost.unpriced_models.join(', ')}` : `Sobre ${int(m.cost.priced_flows)} flujo(s) con tarifa · total ${usd(m.cost.total) ?? '—'}`),
    ),
    card('Flujos terminados', int(f.finished),
      el('div', { class: 'sub' }, `Duración promedio ${dur(m.avg.duration_ms)} · ${int(m.avg.tokens_per_flow)} tokens/flujo`),
      el('div', { class: 'chips' }, Object.entries(f.by_status).map(([k, n]) => el('span', { class: 'chip', text: `${(FINAL[k] || [k])[0]}: ${n}` })))),
  );

  const max = Math.max(1, ...m.by_agent.map((a) => a.total));
  const agents = el('div', { class: 'section' }, el('h2', { text: 'Tokens por agente' }),
    m.by_agent.length ? el('div', { class: 'bars' }, m.by_agent.map((a) => {
      const fill = el('div', { class: 'bar-fill' });
      fill.style.width = `${Math.max(2, (a.total / max) * 100)}%`;
      return el('div', { class: 'bar-row' }, el('span', { text: a.agent }), el('div', { class: 'bar-track' }, fill),
        el('span', { class: 'bar-val num', text: `${int(a.total)} · ${usd(a.cost) ?? 'sin tarifa'}` }));
    })) : el('div', { class: 'empty', text: 'Aún no hay consumo de tokens registrado.' }));

  const head = ['ID', 'Estado', 'Agente', 'Tokens', 'Costo', 'Duración', 'Reint.', 'Actualizado'];
  const table = el('div', { class: 'section' }, el('h2', { text: 'Últimos flujos' }),
    m.recent.length ? el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, head.map((h, i) => el('th', { class: [3, 4, 5, 6].includes(i) ? 'r' : '', text: h })))),
      el('tbody', {}, m.recent.map((r) => {
        const [label, cls] = stateLabel(r);
        return el('tr', {}, el('td', { class: 'num', text: r.id }), el('td', {}, el('span', { class: `state ${cls}`, text: label })),
          el('td', { text: r.agent || '—' }), el('td', { class: 'r num', text: int(r.tokens) }),
          el('td', { class: 'r num', text: usd(r.cost) ?? 'sin tarifa' }), el('td', { class: 'r num', text: dur(r.duration_ms) }),
          el('td', { class: 'r num', text: int(r.retries) }), el('td', { class: 'num', text: hhmmss(r.updated_at) }));
      })))) : el('div', { class: 'empty', text: 'Todavía no hay flujos. Inicia un proceso en PRISMA para verlo aquí.' }));

  const a = m.archive;
  const foot = el('div', { class: 'foot' },
    a && a.enabled
      ? [`Respaldo en tabla log: ${a.ok ? 'activo' : ''} `, a.ok ? null : el('span', { class: 'bad', text: `con error (${a.last_error})` }),
        ` · ${int(a.saved_total)} eventos guardados`, a.last_saved_at ? ` · último ${hhmmss(a.last_saved_at)}` : '']
      : 'Respaldo en tabla log: desactivado', m.skipped_events ? ` · ${int(m.skipped_events)} eventos omitidos por no cumplir el contrato` : '');

  return el('div', {}, kpis, agents, table, foot);
}

let timer = null;
const newState = () => ({ last: null, activity: null, tab: 'agentico' });
const shared = newState(); // un solo estado: la pestaña y los datos sobreviven a cambios de visibilidad
let gen = 0; // generación del ciclo de polling: un ciclo viejo deja de pintar y de reprogramarse

/** Pide un endpoint sin lanzar: devuelve {status, data} o {error} si falló la red. */
async function getJson(cfg, path) {
  try {
    const r = await fetch(apiUrl(cfg, path), { headers: { Authorization: `Bearer ${session.get(STORE.token)}` }, cache: 'no-store' });
    if (!r.ok) return { status: r.status };
    return { status: r.status, data: await r.json() };
  } catch (error) { return { error }; }
}

function paint(state, stale) {
  render(state.last, {
    stale, tab: state.tab, activity: state.activity,
    onTab: (k) => { state.tab = k; paint(state, stale); },
  });
}

async function poll(state) {
  const mine = ++gen;
  clearTimeout(timer);
  if (document.hidden) { timer = setTimeout(() => poll(state), POLL_MS); return; }
  try {
    const cfg = await loadConfig();
    const [mr, ar] = await Promise.all([getJson(cfg, '/api/monitor/metrics'), getJson(cfg, '/api/monitor/activity')]);
    if (mine !== gen) return;
    if (mr.status === 401 || ar.status === 401) { session.del(STORE.token); session.set(STORE.notice, 'Tu sesión expiró. Inicia sesión de nuevo.'); return location.replace('/login'); }
    if (mr.status === 403 || ar.status === 403) return renderForbidden();
    if (mr.data) state.last = mr.data;
    if (ar.data) state.activity = ar.data;
    if (!state.last && !state.activity) throw new Error('sin datos');
    // «sin conexión» solo si falló lo que se está mirando; la otra pestaña conserva su último dato.
    paint(state, !(state.tab === 'uso' ? ar.data : mr.data));
  } catch {
    if (mine !== gen) return;
    if (state.last || state.activity) paint(state, true);
    else screen('Sin conexión', 'No se pudo obtener las métricas del monitor.', el('button', { onclick: () => location.reload(), text: 'Reintentar' }));
  }
  timer = setTimeout(() => poll(state), POLL_MS);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && claims()) poll(shared); });

// ── arranque ────────────────────────────────────────────────────────────────
(async function main() {
  if (location.pathname === '/auth/callback') return handleCallback();
  if (location.pathname === '/login') return claims() ? location.replace('/') : renderLogin();
  if (!claims()) return location.replace('/login');
  poll(shared);
})();
