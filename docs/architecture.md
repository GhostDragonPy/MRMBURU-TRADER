# Decisiones técnicas — v0.2

## Límites

FastAPI y worker comparten contratos y módulos; PostgreSQL es la fuente de verdad.
Redis queda para trabajo temporal. No almacenar allí el Kill Switch ni balances autorizados.
La IA será un productor de propuestas y señales; no tendrá clave de administrador, acceso
SQL ni credenciales de broker. Separar permisos y procesos al incorporar el motor IA.
La separación actual es lógica, no un sandbox para código arbitrario de IA.

El gateway de ejecución rechaza toda llamada. La configuración solo admite `paper` y
`execution_enabled=false`; cambiar `.env` a `live`, `demo`, `funded`, etc. impide arrancar.
La base también exige `accounts.mode='paper'`. No hay endpoint que cambie esos valores.

## Flujo implementado

Señal → snapshot sintético de cuenta → Risk Engine → reglas genéricas de fondeo →
registro atómico de señal/decisión/auditoría. Finaliza allí: no llega a un broker.
Los controles se obtienen de la base; el cliente de investigación no decide límites.
Se bloquea primero la fila del Kill Switch y luego la cuenta al evaluar; la API STOP
usa la misma fila. La base debe ser PostgreSQL para locks de producción: SQLite solo sirve
para pruebas rápidas, no demuestra concurrencia PostgreSQL.

## Esquema

| Tabla | Propósito |
|---|---|
| accounts | Cuenta paper, moneda, perfil y políticas individuales |
| account_snapshots | Equity/balance, fecha del día de riesgo y contadores |
| strategy_versions | Especificación y versión, estados de evaluación |
| signals | Señal, contexto, snapshot usado e idempotencia por cuenta |
| risk_decisions | Razones, riesgo calculado, copia de políticas y reglas |
| kill_switch | Única fila persistente del control global |
| audit_events | Acciones y evaluaciones con actor y fecha |
| journal_entries | Resultado, R, calidad, errores y contexto completo por operación |
| economic_events | Eventos con fuente y fecha de recuperación |
| knowledge_sources | Transcripción, reglas, contradicciones y hash |
| backtest_runs | Split, ventana, dataset hash, supuestos y métricas |
| improvement_proposals | Propuestas separadas de la estrategia |

SQL usa Numeric(24,8) para importes. Las políticas/contextos usan JSON validado al entrar.
Las versiones y propuestas no se promocionan automáticamente. Antes de producción se
necesitarán ACL SQL, inmutabilidad de versiones aprobadas y auditoría resistente a cambios.
No ejecutar código extraído de videos; la extracción futura producirá un DSL declarativo validado.

## Reglas y tiempo

Los porcentajes por defecto son ejemplos conservadores configurables, no asesoría ni reglas
oficiales de FTMO. La zona horaria del perfil determina el día de riesgo; usar IANA para DST.
Un snapshot del día anterior se rechaza. Noticias desconocidas o vencidas bloquean.
Para fase cTrader: verificar elegibilidad FTMO por país/cuenta/plataforma, autenticación,
permisos, límites y reglas oficiales vigentes. No asumir disponibilidad por este diseño.

## Operación

API y worker corren sin root, filesystem de solo lectura y red interna; API solo loopback.
Compose espera servicios saludables y migraciones completadas antes de iniciar la API.
Referencia: https://docs.docker.com/compose/how-tos/startup-order/
Las escrituras se agrupan con transacciones SQLAlchemy:
https://docs.sqlalchemy.org/en/20/orm/session_transaction.html

La migración 0001 contiene DDL congelado; no importa modelos mutables para crear tablas.
`upgrade head` es repetible. La instalación no sobreescribe datos ni elimina volúmenes.
Antes de órdenes paper habrá que añadir ledger, posiciones, reservas de riesgo atómicas,
reconciliación, política de fills y cierre, y pruebas de carreras/duplicados.
