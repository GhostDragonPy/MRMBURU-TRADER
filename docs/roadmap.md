# Roadmap

| Fase | Estado / siguiente entrega |
|---|---|
| 1 Infraestructura | Base y CI preparados; despliegue Docker en Linux por verificar |
| 2 Base de datos | Migración 0001, modelos, auditoría; prueba PostgreSQL en CI |
| 3 Strategy Engine | Contratos y SMA de referencia; siguiente DSL y versiones inmutables |
| 4 Backtesting | Pendiente: fills next-bar, comisiones/spread/slippage, datos sin sesgo, métricas |
| 5 Journal | Esquema + clasificación independiente de PnL; pendiente ingestión de operaciones |
| 6 Risk Engine | Núcleo y evaluación; pendiente ledger, reservas, rollover y reconciliación |
| 7 YouTube | Pendiente: transcripción autorizada, citas temporales, extracción y contradicciones |
| 8 News | Contrato de bloqueo; pendiente proveedor y calendario con frescura garantizada |
| 9 Paper | Modo cerrado; pendiente simulador y ejecución ficticia completa |
| 10 cTrader | Pendiente adaptación y validación oficial; ninguna credencial requerida ahora |
| 11 FTMO Demo | Pendiente disponibilidad/reglas verificadas y pruebas de reconexión |
| 12 Challenge simulado | Pendiente reproducción de límites, horarios y casos extremos |
| 13 Challenge real | Bloqueado; requiere aprobación explícita y gates de validación |
| 14 Funded | Bloqueado; aprobación explícita independiente |

Backtesting: TRAIN/VALIDATION/TEST cronológicos, test reservado y walk-forward.
Registrar dataset hash, versión, semilla, parámetros y supuestos. Evitar look-ahead,
selección retrospectiva de instrumentos y reutilización del test para ajuste.
Métricas: WR, PF, drawdown equity, EV/R, Sharpe con frecuencia explícita, rachas,
rendimiento por sesión/activo/hora y con/sin noticias. Reportar tamaño de muestra.

Promoción: análisis → propuesta → backtest → validación → walk-forward → paper →
aprobación humana → versión aprobada. Nada de autoedición de una estrategia productiva.
