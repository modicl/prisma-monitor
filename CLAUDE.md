# CLAUDE.md — prisma-monitor

Monitor en vivo del workflow agéntico: **flujos corriendo, tokens usados y costo promedio**.
Diseño completo: `../docs/superpowers/specs/2026-09-29-prisma-monitor-kafka-design.md`.
Puesta en marcha local: `../LOCAL.md` (sección «Monitor con Kafka»).

- **Stack:** Python 3.12 + FastAPI + `aiokafka` + `asyncpg`. Puerto **3020**. Rama principal: **`cloudnative`**.
  El pipeline (`.github/workflows/deploy-ecr.yml`) existe, pero **no hay infraestructura desplegada**: hoy solo corre en
  el compose local. La infraestructura propuesta está en `../prisma-infra/07-monitor.tf.pendiente` (sin aplicar).
- **Flujo:** `prisma_workflow` publica eventos v1 al topic `prisma.agent-events` (Kafka); este servicio los
  consume y los muestra. Sin base de datos para las métricas: al arrancar relee el topic completo.
- **Front:** `web/` (HTML/CSS/JS plano, sin build), servido por el mismo FastAPI. Login PKCE contra Cognito.

## Estructura

| Archivo | Rol |
|---|---|
| `monitor/events.py` | Contrato de eventos v1. **Único** lugar que valida un mensaje. |
| `monitor/aggregator.py` | Estado en memoria; función pura del historial de eventos. |
| `monitor/pricing.py` | Tarifas (`MODEL_PRICES`) y costo. Sin tarifa ⇒ `None`, nunca 0. |
| `monitor/consumer.py` | Lee el topic desde el inicio, sin grupo ni commits (`replaying` mientras se pone al día). |
| `monitor/archiver.py` + `store.py` | Respaldo append-only en `monitor.agent_event_log` (consumer group propio). |
| `monitor/auth.py` | Guard de Cognito: solo `SUPERADMIN`. |
| `monitor/main.py` | Rutas, cabeceras de seguridad y front estático. |
| `scripts/emit_sample_events.py` | Eventos sintéticos para probar sin gastar cuota de Gemini. |

## El front y la API se despliegan por separado

| | Local (compose) | Producción |
|---|---|---|
| Front (`web/`) | Lo sirve el propio monitor | **AWS Amplify** (el HTTPS lo pone Amplify) |
| API (`/api/monitor/*`) | El monitor, mismo origen | ECS (servicio propio) detrás del API Gateway |
| `SERVE_FRONT` | `true` (por defecto) | `false`: la API no publica HTML ni `/config.json` |
| CORS | No hace falta (mismo origen) | **Solo el API Gateway** (nunca el monitor) |
| CSP | La pone el monitor | La pone Amplify (`custom_headers` en Terraform) |
| `config.json` | Lo genera el monitor (`apiBase` vacío) | Lo genera el build de Amplify con la URL del gateway |

- El front lee `apiBase` de `/config.json`: vacío = mismo origen; en Amplify, la URL del API Gateway.
- **El monitor no debe emitir CORS jamás**: si lo hicieran él y el gateway, el navegador vería un
  `Access-Control-Allow-Origin` duplicado y rechazaría la respuesta (hay un test que lo impide).
- `index.html` referencia `/static/app.js` y `/static/style.css`; el build de Amplify copia los assets a `dist/static/`
  para que la misma `index.html` sirva en los dos modos.
- Al desplegar el front en Amplify hay que registrar su URL en Cognito (`front_urls` de `prisma-infra`).

## Comandos

```bash
# tests (Docker, sin instalar nada local)
docker build -t prisma-monitor-dev -f - . <<< $'FROM python:3.12-slim\nWORKDIR /m\nCOPY requirements*.txt ./\nRUN pip install -r requirements-dev.txt'
docker run --rm -v "$PWD:/m" -w /m prisma-monitor-dev python -m pytest -q
# la prueba contra Postgres real se activa con TEST_DATABASE_URL
```

## Reglas (no las rompas)

- **Sin PII en los eventos, nunca.** El contrato solo lleva ids aleatorios, contadores y nombres de agente. Este
  servicio muestra datos operativos de un sistema que procesa diagnósticos de menores.
- **Seguridad en el servidor.** El BFF no valida tokens y ocultar la página no protege nada: `require_superadmin`
  es lo que impide el acceso. Rol por lista blanca sobre `cognito:groups` (el grupo `<pool>_Google` **no** es un rol);
  sin grupo real ⇒ `PENDING` ⇒ 403. El token va solo en el header, nunca en la URL.
- **Los eventos llevan tokens, no costo.** El costo se calcula aquí con `MODEL_PRICES`; así una tarifa nueva se
  aplica a todo el historial al reiniciar.
- **La tabla log es append-only.** Nunca `UPDATE` ni `DELETE`. Se identifica por `(topic, partición, offset)`.
- **El front pinta con `textContent`**, jamás `innerHTML` (la CSP además lo bloquea).
- `/docs` y `/openapi.json` están deshabilitados a propósito.
