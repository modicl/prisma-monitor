# prisma-monitor

Monitor en vivo del workflow de P.R.I.S.M.A. Muestra los flujos que estan corriendo, los tokens consumidos, el costo promedio por flujo y la actividad de uso de la plataforma. Solo lo puede ver un usuario con rol `SUPERADMIN`.

P.R.I.S.M.A. es un sistema para docentes chilenos que genera rubricas y material de evaluacion adaptado para estudiantes con Necesidades Educativas Especiales. Este repositorio es la pieza de observabilidad del sistema; no participa en la generacion de documentos.

## Como funciona

```
prisma_workflow --eventos v1--> Kafka (prisma.agent-events) --> prisma-monitor :3020
bff-prisma ----actividad-----> Kafka (prisma.activity-events) -->        |
                                                                         +--> API /api/monitor/*  --> front (web/)
                                                                         +--> archivador --> Postgres monitor.agent_event_log
```

- El workflow y el BFF publican eventos a Kafka. Ninguno depende de Kafka: si no responde, descartan el evento y siguen.
- El monitor lee `prisma.agent-events` desde el inicio, sin grupo ni commits, y reconstruye sus metricas en memoria. Las metricas son una funcion pura del historial.
- `prisma.activity-events` se lee desde el final y alimenta la vista de uso en vivo.
- Un archivador (consumer group propio) copia cada evento a una tabla de solo insercion en Postgres, identificada por `(topic, particion, offset)`. Si Kafka se pierde, el historial se conserva.
- Los eventos no llevan datos personales: solo identificadores aleatorios, contadores y nombres de agente. Llevan tokens y no costo; el costo se calcula aqui con las tarifas de `MODEL_PRICES`.
- La autenticacion se valida en el servidor contra Cognito (token ID, JWKS) y exige el grupo `SUPERADMIN`.

## Stack

Python 3.12, FastAPI, aiokafka, asyncpg. Front estatico en `web/` (HTML, CSS y JavaScript sin build) con login PKCE contra Cognito. Puerto 3020.

## Estructura

| Ruta | Funcion |
|---|---|
| `monitor/events.py` | Contrato de eventos v1. Unico lugar que valida un mensaje. |
| `monitor/aggregator.py` | Estado en memoria a partir del historial de eventos. |
| `monitor/pricing.py` | Tarifas y calculo de costo. Sin tarifa muestra "sin tarifa", nunca 0. |
| `monitor/consumer.py` | Consumidor de `prisma.agent-events`. |
| `monitor/activity.py`, `activity_consumer.py` | Contrato y consumidor de actividad. |
| `monitor/archiver.py`, `store.py` | Respaldo append-only en Postgres. |
| `monitor/auth.py` | Guard de Cognito, solo `SUPERADMIN`. |
| `monitor/main.py` | Rutas, cabeceras de seguridad y front estatico. |
| `web/` | Front del monitor. |
| `scripts/` | Emisores de eventos sinteticos para probar sin consumir cuota del LLM. |
| `tests/` | Pruebas con pytest y una prueba de JavaScript del front. |

## Configuracion

Variables de entorno (ver `.env.example`):

| Variable | Descripcion |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | Direccion de Kafka. Vacia: el monitor arranca sin consumir. |
| `KAFKA_TOPIC`, `KAFKA_ACTIVITY_TOPIC` | Topicos de eventos del workflow y de actividad del BFF. |
| `MONITOR_DATABASE_URL` | Postgres del archivador. Vacia: sin respaldo, las metricas siguen funcionando. |
| `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_DOMAIN` | Mismo pool y app client que `prisma-front`. |
| `MODEL_PRICES` | Tarifas en USD por millon de tokens, por modelo. |
| `SERVE_FRONT` | `true` en local (el servicio sirve el front); `false` en produccion, donde el front va en Amplify. |

## Ejecucion y pruebas

```bash
pip install -r requirements.txt
uvicorn monitor.main:app --port 3020
```

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Para ver datos sin ejecutar un flujo real: `python scripts/emit_sample_events.py` y `python scripts/emit_sample_activity.py`.

## Despliegue

El front y la API se despliegan por separado. El front se publica en AWS Amplify y la API corre como servicio ECS detras del API Gateway, con `SERVE_FRONT=false`. El CORS lo aplica solo el API Gateway; el monitor nunca debe emitirlo. La infraestructura esta definida en el repositorio `prisma-infra`.

## Reglas

- Los eventos nunca llevan datos personales. El sistema procesa informacion de menores de edad.
- La seguridad se aplica en el servidor: ocultar la pagina no protege nada.
- La tabla de respaldo es de solo insercion: nunca `UPDATE` ni `DELETE`.
