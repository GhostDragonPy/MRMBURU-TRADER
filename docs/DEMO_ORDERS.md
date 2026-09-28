# Demo-orders (cTrader DEMO) — procedimiento de rollout

Paper sigue siendo el default. `EXECUTION_ENABLED` permanece `false` (LIVE).
`ALLOW_LIVE_TRADING=false`. `DEMO_EXECUTION_ENABLED=false` por defecto.
La cuenta `48803059` está denegada. Host: `demo.ctraderapi.com:5035`.

## Permiso trading (no inventado)

El SDK expone `ProtoOAGetAccountListByAccessTokenRes.permissionScope`:
`SCOPE_TRADE` → **VERIFIED**; `SCOPE_VIEW` u omitido → **UNVERIFIED**.
Canary/enabled no envían órdenes mientras sea UNVERIFIED.

## Shadow (DEMO_EXECUTION_ENABLED puede seguir en false)

1. `TRADING_MODE=demo-orders`, `CTRADER_ENVIRONMENT=demo`,
   `DEMO_CTRADER_ACCOUNT_ID` DEMO (`isLive=false`). Token OAuth en Redis/env
   (nunca imprimirlo).
2. Preflight: `python -m apps.demo_probe` (socket real read-only).
3. Avanzar `disabled → shadow`. El worker abre y conserva la sesión SDK,
   heartbeats cada 10 s, registra would-orders, **cero** NewOrder/Close/Amend.
4. `/demo_status` muestra socket, auth y preflight sanitizados.

Órdenes (canary/enabled) exigen a la vez:
`TRADING_MODE=demo-orders`, `DEMO_EXECUTION_ENABLED=true`, rollout canary|enabled,
preflight VERIFIED vigente, emergency stop inactivo, señal fresca y Risk Engine.

La barrera central `TradingMessageBarrier` limita mensajes de trading:
probe/disabled/shadow = 0; canary = 1 `ProtoOANewOrderReq` persistente.
