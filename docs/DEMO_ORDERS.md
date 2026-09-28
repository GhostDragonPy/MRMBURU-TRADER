# Demo-orders (cTrader DEMO) — procedimiento de rollout

Paper sigue siendo el default. `EXECUTION_ENABLED` permanece `false` y está
reservado para LIVE (bloqueado). `ALLOW_LIVE_TRADING=false`.
`DEMO_EXECUTION_ENABLED=false` por defecto. La cuenta `48803059` está denegada
de forma permanente. Host permitido: `demo.ctraderapi.com:5035`.

No hay credenciales en `docker-compose.yml`. No desplegar esta fase.

## Estados (sin saltos)

1. **disabled** — transporte sin órdenes. Esses paper evalúa con normalidad.
2. **shadow** — Esses evalúa; se persiste la orden que se habría enviado
   (volumen paper, SL, TP, razón). Cero `ProtoOANewOrderReq`.
3. **canary** — como máximo **una** orden total (contador persistente), volumen
   mínimo del símbolo. Si el mínimo supera el riesgo 0.25%, se bloquea.
4. **enabled** — señales **nuevas** (posteriores a `armed_at`) bajo Risk Engine.

Avance: `disabled → shadow → canary → enabled`. Confirmación administrativa
obligatoria. Discord no puede elegir cuenta, host ni LIVE.

## Shadow (operador)

1. Dejar `TRADING_MODE=paper` y `DEMO_EXECUTION_ENABLED=false` en producción.
2. En un entorno no productivo: `TRADING_MODE=demo-orders`,
   `DEMO_EXECUTION_ENABLED=true`, `ESSES_BROKER_EXECUTION=true`,
   `DEMO_CTRADER_ACCOUNT_ID` de una cuenta DEMO (`isLive=false`).
3. Token OAuth `scope=trading` (nunca imprimir el token).
4. Preflight read-only:
   `docker compose exec api python -m apps.demo_probe`
   (esta fase el comando exige sesión inyectada; sin activación de sockets
   de órdenes). Salida `PASS`/`FAIL` sanitizada.
5. Avanzar rollout a `shadow` (un paso, auditado). `armed_at` queda fijado.
6. Observar `/demo_status`. No debe haber `broker_order_id` de envío.

Luego, solo si el preflight sigue vigente: `shadow → canary` (una orden mínima)
y más tarde `canary → enabled`. Reiniciar el proceso **no** reinicia el
contador canary ni reproduce señales históricas.

## Healthcheck

El worker usa `python -m apps.demo_health`. Con paper / demo desactivado solo
exige `worker:heartbeat`. Si `TRADING_MODE=demo-orders` y
`DEMO_EXECUTION_ENABLED=true`, exige también `demo:preflight:ok` en Redis.

## Variables nuevas (sin valores secretos)

- `SIGNAL_MAX_AGE_SECONDS` (default 90)
- `DEMO_PREFLIGHT_TTL_SECONDS` (default 86400)
