# Esses research v1 — EURUSD, simulación local

Implementación objetiva basada en las transcripciones aportadas. No es una copia
exacta de decisiones discrecionales ni tiene rentabilidad validada. No envía órdenes
a cTrader, ni siquiera demo: los fills, stops y saldo se registran en PostgreSQL.

## Reglas de esta versión

- Contexto D1/H4/H1/M15; ejecución M1. M5 se conserva para observación.
- Pivots estrictos de dos velas a cada lado, disponibles solo al cerrarse las dos
  velas posteriores. Bias = último cruce por cierre del pivot conocido más reciente.
- H1 y M15 deben coincidir. H4 se registra, sin veto absoluto. D1 aporta máximo/mínimo
  previo. No hay puntuación discrecional ni cambios retrospectivos de rango.
- Barrido M1 de un pivot M15 confirmado o extremo del último D1 cerrado, con regreso
  por cierre. Caducidad cinco velas; niveles ya barridos dentro del historial disponible
  se descartan. Historial limitado a 200 velas por marco.
- El barrido toca FVG u OB H1/M15 sin contacto previo observable. FVG de tres velas,
  separación mínima 0.2 pip. OB = cuerpo de última vela contraria, hasta diez velas
  antes del BOS. Sin variantes de series/extremos/refinamiento dinámico.
- Confirmación alternativa: IFVG (único FVG contrario no invertido en las diez velas
  anteriores al barrido); CISD (apertura inicial de secuencia contraria consecutiva,
  doji rompe la secuencia); BOS seguido de un retesteo posterior de FVG de la pierna.
- Stop más allá del extremo desde el barrido, con margen 1 pip. Objetivo = pivot M15
  contrario no tomado más cercano. FVG M15 contrario fresco antes del objetivo bloquea.
- RR mínimo 1.5 antes y después de spread/slippage: es una decisión de esta versión,
  no una regla universal atribuida a Fede. Comisión y slippage siguen siendo supuestos.
- Riesgo máximo 0.25% por operación y tope de exposición nominal 1x. Esto puede reducir
  el riesgo efectivo. Dos entradas/día (también cuentan BE/ganadoras), máximo asignado
  1% diario, una posición simultánea. No reutiliza el mismo barrido tras cerrar.
- Entradas 09:30–11:00 America/New_York (DST automático). Se fuerza salida a partir
  de 11:00 en la siguiente cotización válida: simplificación explícita para esta prueba.
- BE de precio solo tras FVG posterior a entrada retesteado y cierre más allá de pivot
  posterior confirmado. No amplía stops; comisiones pueden dejar pérdida en BE.
- Falta de cotizaciones >60 s con posición abierta pausa nuevas entradas hasta revisión;
  las salidas usan la siguiente cotización recibida, nunca inventan fills en el hueco.
- Calendario EUR/USD revisado por día NY: bloquea 30 min antes/después de eventos.
  Sin calendario bloquea entradas, salvo waiver de investigación explícito. No hay
  proveedor automático de noticias incorporado. `apps.esses_news` carga JSON por stdin.
- SMT, Fibonacci/equilibrium, reentradas simultáneas y posición adicional no se incluyen
  en esta variante inicial de Forex. No se exige ninguna de ellas para describirla como
  una variante de los ejemplos Forex aportados.

## Datos y conexión

Un recolector único, con lock Redis, conecta exclusivamente a demo de 09:15 a 11:10 NY.
Valida account ID e isLive en la respuesta oficial de cuentas antes de autenticar cuenta.
Carga historia una vez al conectar y se suscribe a spots y seis marcos de velas cerradas.
API/worker leen caché Redis. Reconexiones esperan cinco minutos y recargan historia.
Presupuesto móvil compartido: 60 mensajes/min y 1000/24h, incluye heartbeat cada 9s.
Sesión normal de 115min usa aproximadamente 767 heartbeats más mensajes de arranque.
No es garantía sobre reglas del bróker ni contempla tráfico de otras aplicaciones.
Redis debe conservar su volumen/AOF; perder contadores requiere revisar antes de reanudar.

Fuentes del protocolo consultadas:
https://help.ctrader.com/open-api/connection/
https://help.ctrader.com/open-api/account-authentication/
https://help.ctrader.com/open-api/messages/

## Prueba y observación

`python -m apps.esses_check` prueba Redis real y concurrencia SIN contactar cTrader.
`python -m apps.esses_replay snapshots.jsonl --output resultados.jsonl` usa snapshots
de quotes y velas, sin red. Con `--allow-unknown-news` documenta el waiver en entradas.
No asume trayectoria intravela: para estudiar fills hace falta historial de cotizaciones.
Pruebas unitarias usan escenarios sintéticos, no demuestran eficacia financiera.

Estado HTTP existente: `/paper/v04/status`, autenticación research habitual.
Incluye estrategia, colector, entorno demo, análisis, posiciones y eventos.
El endpoint conserva su nombre por compatibilidad. Si faltan credenciales/permiso,
conectividad o datos, se muestra error y no se simulan entradas.

No se verificó una conexión a tu demo desde este entorno; la VPS debe disponer del
ctidTraderAccountId demo y token autorizado. Una cuenta de evaluación que cTrader
clasifique como live puede utilizarse explícitamente solo como fuente de precios con
`--live-paper-account`; la ejecución sigue limitada al ledger paper local. El instalador no convierte una cuenta
live/FTMO en demo ni reactiva una cuenta suspendida.
