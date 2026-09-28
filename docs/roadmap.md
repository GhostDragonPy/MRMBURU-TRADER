# Roadmap

Estado alineado con el código en `feature/discord-control` (paper only, `execution_enabled=false`).

| Fase | Estado |
|---|---|
| 1 Infraestructura | Hecho: Docker, CI, Caddy `trader.acshop.shop` |
| 2 Base de datos | Hecho: migraciones `0001`–`0003` (ledger paper + Discord) |
| 3 Strategy Engine | Hecho: SMA de referencia y **Esses v1** (default `PAPER_STRATEGY=esses-v1`). Pendiente: DSL e inmutabilidad de versiones |
| 4 Backtesting | Pendiente: next-bar, walk-forward, dataset hash, métricas con muestra |
| 5 Journal | Parcial: esquema + clasificación; ingestión automática de todas las vías paper aún incompleta |
| 6 Risk Engine | Hecho el núcleo y kill switch. Pendiente: reservas/rollover/reconciliación unificados entre los tres ledgers |
| 7 YouTube | Pendiente |
| 8 News | Contrato de bloqueo hecho; calendario Esses se carga a mano (`apps.esses_news`). Sin proveedor automático |
| 9 Paper | Hecho: simulador v0.4, pipeline clásico, sandbox Discord. Scheduler **apagado** por defecto |
| 10 cTrader | Parcial: Open API lectura (bid/ask, OHLC, cuenta). Sin órdenes. OAuth Account info; app Spotware no necesariamente Active |
| 11 Discord control | Hecho: slash paper + GhostDragon; auth por IDs; perfil Compose `discord` |
| 12 Dashboard | Pendiente (`apps/dashboard` solo README) |
| 13 FTMO Demo / challenge simulado | Pendiente |
| 14 Challenge real / Funded | Bloqueado; aprobación explícita independiente |

Tres ledgers paper que no se mezclan: (1) `POST /paper/accounts/{id}/run` SMA+journal, (2) ledger v0.4/Esses, (3) `discord-sandbox`.

Backtesting futuro: TRAIN/VALIDATION/TEST cronológicos, test reservado y walk-forward.
Registrar dataset hash, versión, semilla, parámetros y supuestos. Evitar look-ahead.

Promoción: análisis → propuesta → backtest → validación → walk-forward → paper →
aprobación humana → versión aprobada. Nada de autoedición de una estrategia productiva.
Nada de envío de órdenes a cTrader en estas fases.
