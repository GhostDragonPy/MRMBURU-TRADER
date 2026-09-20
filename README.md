# MRMBURU TRADER · v0.2

Base de investigación de trading automatizado. Continúa `trading-ai-v0.1`.
**Solo paper. No envía órdenes, ni siquiera demo. No contiene integración de broker.**

## Arrancar en Linux

Requiere Python 3 y Docker Engine con Docker Compose v2.

```bash
cd mrmburu-trader
python3 scripts/init_env.py
docker compose up -d --build
curl --fail http://127.0.0.1:8000/ready
```

`init_env.py` crea `.env` con claves aleatorias y permisos 0600; no lo sobrescribe.
No reutilizar el `.env` de v0.1: ahora se separan claves de administrador e investigación.
PostgreSQL y Redis no publican puertos. La API escucha en `127.0.0.1:8000`.
Raíz `/` (JSON con enlaces), liveness `/health`, readiness `/ready`, OpenAPI `/docs`.
Las rutas de datos/control requieren `X-API-Key`.
El archivo `.env` nunca se incorpora al repositorio ni a la imagen Docker.

Producción (este host): Caddy termina TLS en `https://trader.acshop.shop` y hace
proxy a `127.0.0.1:8000`. Plantilla: `deploy/Caddyfile.trader.acshop.shop`.

### Detener / reanudar evaluación paper

```bash
docker compose exec api python -m apps.cli stop --reason 'Parada manual'
docker compose exec api python -m apps.cli resume-paper --reason 'Pruebas supervisadas'
```

El Kill Switch comienza activado, queda persistido en PostgreSQL y no se limpia al reiniciar.
La reanudación permite evaluar escenarios; **no habilita ejecución**.
STOP impide nuevas aprobaciones. En esta versión no existen posiciones que liquidar;
una futura acción de cerrar posiciones será distinta y explícita.

### Actualizar

```bash
git pull --ff-only
docker compose build
docker compose run --rm migrate
docker compose up -d
```

Antes de futuras migraciones sobre datos útiles, hacer y verificar un backup.
No ejecutar `docker compose down -v` salvo que se quiera eliminar los datos locales.

## Implementado

- FastAPI con liveness/readiness, claves separadas de administrador e investigación.
- PostgreSQL + migración Alembic versionada; Redis interno; worker con heartbeat.
- Contratos validados con Decimal, fechas conscientes de zona horaria y rechazo de NaN/Infinity.
- Estrategia SMA de referencia con velas cerradas; genera señales, no órdenes.
- Risk Engine determinista, separado de la IA; controles por cuenta y perfil genérico de fondeo.
- Registro de señales, decisiones, políticas usadas, contexto y acciones de control.
- Kill Switch persistente por API/CLI; ausencia del control implica bloqueo.
- CI con pruebas unitarias/API, migraciones PostgreSQL y arranque Docker.
- IA de investigación DeepSeek (`POST /research/ai/propose`); series FRED; cTrader OAuth configurado **sin envío de órdenes**.

## Probar el flujo

1. Con `ADMIN_API_KEY`, `POST /accounts` crea una cuenta deshabilitada.
2. `POST /accounts/{id}/synthetic-state` carga balance/equity/contadores ficticios actuales.
3. `POST /research/strategies/sma/signal` calcula una señal sobre velas suministradas.
4. `POST /research/accounts/{id}/evaluate` evalúa la señal junto con un contexto ficticio.
5. `GET /research/decisions` muestra hasta 100 decisiones recientes.
6. `POST /control/stop` y `/control/resume-paper` exigen clave de administrador.

Ver el ejemplo ejecutable `python -m apps.demo` dentro del contenedor: crea una cuenta
sintética y demuestra el bloqueo inicial. No cambia el Kill Switch.

```bash
docker compose exec api python -m apps.demo
```

La API no permite cambiar límites ni reglas con la clave de investigación. No entregar
la clave de administrador a un agente IA. No hay ruta para cambiar el modo o enviar órdenes.

## Estructura

- `apps/api`, `apps/worker`, `apps/cli.py`: puntos de entrada.
- `apps/dashboard`: contrato pendiente de interfaz; aún sin dashboard.
- `core`: configuración, contratos, persistencia.
- `services/strategy_engine`, `risk_engine`, `prop_firm_engine`, `execution_engine`, `journal`.
- `infrastructure/postgres/migrations`: esquema versionado.
- `tests`, `.github/workflows/ci.yml`: validación.
- `docs/architecture.md`, `docs/roadmap.md`: decisiones y siguientes fases.

Los módulos Python usan `_` en vez de `-` para ser importables. Se mantienen en un
monolito modular; separar servicios desplegables cuando la carga lo justifique.

## Desarrollo

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
python scripts/check_secrets.py
```

`requirements.lock` fija las dependencias transitivas de ejecución verificadas con Python 3.12.
Docker y CI instalan este lock. Actualizarlo conscientemente al cambiar dependencias.
La prueba PostgreSQL usa exclusivamente `TEST_POSTGRES_URL` hacia una base **desechable**;
ejecuta upgrade/downgrade y nunca debe apuntar a producción.

## Límites de esta versión

- Los precios, costes y estados son escenarios suministrados, no datos de mercado verificados.
- `allowed=true` es una evaluación histórica del escenario, nunca permiso para una orden.
- Una clave de idempotencia devuelve la decisión original aunque STOP haya cambiado;
  el resultado siempre es `executable=false`.
- El sizing usa cantidad × distancia de stop × valor por unidad de precio en moneda de cuenta.
  No asumir que equivale a lotes de cTrader: la conversión de instrumentos/FX llegará con el adaptador.
- Perfil de fondeo genérico con pérdida total estática; no es una implementación certificada de FTMO.
- Equity suministrada debe incluir PnL flotante y costes; el motor además reserva riesgo abierto
  y nuevo riesgo de forma conservadora. El rollover diario se valida, no se calcula automáticamente.
- El worker solo confirma disponibilidad. Las tareas asíncronas aún no están implementadas.
- No hay backtesting, descarga YouTube, feed de noticias, fills paper ni aprendizaje automático.
- Las tablas de journal/backtests/conocimiento son esquema inicial para esas fases.
- No desplegar públicamente sin TLS, sesiones, límites de solicitudes y endurecimiento adicional.

## Publicación privada en GitHub

Nombre técnico previsto: `GhostDragonPy/MRMBURU-TRADER` (título: MRMBURU TRADER).
Si el repositorio aún no existe, el propietario puede crear y subir esta base desde Linux:

```bash
gh auth login
# Si se partió del ZIP sin historial local:
git init -b main
git add .
git commit -m 'feat: paper-only modular foundation v0.2'
gh repo create GhostDragonPy/MRMBURU-TRADER --private --source=. --remote=origin --push
```

No introducir tokens en comandos ni archivos. Autenticarse mediante el flujo de GitHub CLI.
CI está preparado; CD a Linux se configura después de elegir servidor y acceso.
