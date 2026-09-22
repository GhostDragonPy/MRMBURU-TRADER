# v0.4: simulador EURUSD/USD (candidato para validación en VPS)

## Alcance

- cTrader entrega datos en solo lectura; NO se envían órdenes, modificaciones ni cierres al broker.
- Fuente Live conservada como en el VPS. `trading_mode=paper`, `execution_enabled=false`.
- Cuenta local nueva de USD 100.000, independiente del saldo, posiciones y journal legado de cTrader.
- Una posición como máximo. Estrategia de referencia SMA 5/20 en velas M15 cerradas.
- Sondeo de cotizaciones cada 10 segundos más latencia de consultas. No es streaming tick a tick.
- Apertura al ask/bid observado, cierre al bid/ask observado; stop y objetivo no garantizan precio.
- Volumen expresado en unidades EUR (no lotes); restricciones min/max/paso obtenidas del símbolo.
- Para EURUSD y cuenta USD, PnL = cambio de precio × unidades EUR; valor por pip = unidades × 0,0001.
- Tope de riesgo por entrada: el menor entre la política de cuenta y 0,25% del equity; notional máximo 1× equity.
- Supuestos de costos: USD 3,50 por 100.000 unidades por lado y 0,1 pip de slippage por lado.
  NO son comisiones verificadas del broker. Spread observado; sin swaps modelados.
- Sin nuevas entradas fuera de 01:00–20:00 UTC, lunes a viernes. Se liquida paper al primer
  precio fresco observado desde las 20:00 UTC para evitar mantener posiciones de un día a otro.
- Balance, equity, racha, límites diarios, cursor de vela y posición persisten en PostgreSQL.
- Eventos de apertura/cierre/bloqueo/pausa son atómicos con el saldo. Bloqueos de fila serializan ciclos.
- La versión anterior `/paper/accounts/{id}/run` se bloquea para la cuenta configurada v0.4.

## Límites importantes

Esto valida funcionamiento técnico, NO rentabilidad ni cumplimiento FTMO. Un `signal:null`
no es evidencia de que la estrategia sea rentable. El SL porcentual SMA es ilustrativo.
Los stops se evalúan con cotizaciones muestreadas: puede haberse tocado una barrera entre
dos muestras. No reconstruimos ticks perdidos ni inventamos ejecuciones históricas.
Un hueco >60 segundos con posición abierta pausa nuevas entradas y marca cierres como
afectados por el hueco. La posición existente sigue pudiendo cerrar con un precio fresco.
Se necesita revisión manual y estar sin posición para reconocer el hueco.

Noticias: `news_known=false` siempre. Por defecto se bloquean entradas sin calendario.
Solo para experimentar en paper se puede decidir explícitamente
`PAPER_ALLOW_UNKNOWN_NEWS=true`: elimina exclusivamente el veto NEWS_UNKNOWN,
no finge una verificación ni modifica otros límites. Ese permiso queda en cada apertura.
No hay calendario integrado, renovación automática OAuth, estadísticas de rentabilidad
validadas ni envío de resúmenes diarios. Si caduca el token, se registra el error y no se
confirman nuevos movimientos; la recuperación requiere reautorizar. No ocultamos ese límite.

## Pruebas realizadas

92 aprobadas, 1 omitida (PostgreSQL no disponible), 3 advertencias de dependencias.
Pruebas locales con Python 3.12 y dependencias en `/tmp/mrmburu-venv`.
`pip check` sin conflictos. Migración SQLite: subir, repetir, comprobar esquema, bajar/subir.
Cobertura de compras/ventas, costos, stop con gap, objetivo, límites, una posición,
idempotencia, rollback, reinicio lógico, recuperación del worker y autorización de endpoints.
Se comprueba hora de cierre de velas y rechazo de cotización sin timestamp del broker.
Se comprueba bloqueo de mensajes de abrir, modificar y cerrar órdenes cTrader.

Pendiente en VPS: build Docker, migración y concurrencia PostgreSQL real, lectura actual
cTrader y prueba prolongada del scheduler. La prueba PostgreSQL solo debe ejecutarse
en una BASE DE PRUEBAS DESECHABLE: hace downgrade y borra sus tablas. NUNCA usar producción.

## Instalación en dos fases (sin commit automático)

1. Extraer el paquete fuera del repositorio. En el VPS limpio ejecutar:

   `python3 /ruta/al/paquete/check_apply.py /root/Sistemas/mrmburu-trader`

   Esto solo comprueba hashes y `git apply --check`. Si falla, no forzar ni reemplazar archivos.
   Para aplicar después de revisar: mismo comando con `--apply` al final.

2. Ejecutar `python3 /ruta/al/paquete/validate_docker.py /root/Sistemas/mrmburu-trader`.
   Construye imagen separada y ejecuta tests sin `.env`, redes Compose, volúmenes de DB
   ni credenciales de producción. No reinicia el despliegue. Requiere Docker e Internet.

3. Revisar `git diff --check` y `git diff`. El commit será manual solo después de revisión.

4. Antes del despliegue: guardar backup recuperable de PostgreSQL, confirmar que no hay
   otras modificaciones y mantener `PAPER_SCHEDULER_ENABLED=false`. Construir API/worker
   y ejecutar migración 0002, luego desplegar y comprobar `/ready` y `/health` (0.4.0).
   No ejecutar rollback de migraciones sobre datos que se quieran conservar.

5. Crear cuenta local con `docker compose exec -T api python -m apps.paper_setup`.
   Devuelve PAPER_ACCOUNT_ID; la cuenta queda DESHABILITADA. No cambia el stop global.
   Configurar ese ID en `.env` sin compartirlo ni subir el `.env` al repositorio.
   Elegir expresamente si se acepta operar paper sin filtro de noticias. No modificar
   `CTRADER_ACCOUNT_ID=48803059`, ni TRADING_MODE ni EXECUTION_ENABLED.

6. Recrear API/worker para leer configuración. Endpoints autenticados:

   - GET `/paper/v04/status` (research): saldo, posición, hasta 100 eventos y estado worker.
   - POST `/paper/v04/enable-account` (admin, JSON `reason`): habilita solo la cuenta local.
   - POST `/control/resume-paper` (admin, JSON `reason`): quita stop global solo tras revisar.
   - POST `/paper/v04/acknowledge-gap` (admin, JSON `reason`): reconoce hueco, solo estando plano.
   - POST `/control/stop` (admin, JSON `reason`): impide nuevas entradas; no cancela seguimiento de salidas.

7. Activar PAPER_SCHEDULER_ENABLED=true y recrear worker tras completar la validación.
   Revisar last_success, last_error, pausa y eventos. El healthcheck infraestructura NO
   garantiza éxito del simulador: consultar `/paper/v04/status`. No crear cron paralelo.

## Base y preservación

El parche incremental parte de v0.3, con versión 0.3.0 y host live.ctraderapi.com,
equivalente a los cambios reportados en 2f63843 y 18fd411 sobre 05e15c9.
No se afirma haber descargado esos commits: la base fue reconstruida y se compara por hash.
No incluye secretos, `.env`, dependencias nuevas, datos ni commits. No modifica el índice Git.
Si cualquier archivo difiere, el verificador se detiene antes de aplicar.

Referencia de la corrección de velas: el campo cTrader utcTimestampInMinutes es la apertura,
no el cierre: https://help.ctrader.com/open-api/model-messages/#protooatrendbar
