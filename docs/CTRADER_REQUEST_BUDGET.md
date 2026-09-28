# Presupuesto compartido cTrader — actualizado para Esses v1

La configuración de red sigue desactivada por defecto. El instalador puede activar
una fuente demo después de validar cuenta, autenticación e instrumento.

API y worker leen Redis. Un único recolector mantiene una conexión entre 09:15 y
11:10 de Nueva York; carga históricos al arrancar y recibe spots/velas por suscripción.
Todos los mensajes salientes del transporte (incluidos autenticaciones y heartbeats)
consumen un presupuesto móvil de 60/minuto y 1000/24 horas por cuenta. Redis usa su
propia hora y un script Lua atómico; ante fallos deniega el envío. No se reembolsan
intentos que podrían haberse transmitido. También hay espera de 5 min al reconectar.

Estos son límites internos; no certifican cumplimiento de FTMO ni cuentan otras
aplicaciones o llamadas HTTP OAuth. Redis debe conservar claves/AOF: pérdida o
borrado de contadores exige revisión antes de reactivar.

La sesión normal genera aproximadamente 767 heartbeats y los mensajes de arranque.
Si el presupuesto se agota o falta conectividad, pueden faltar precios para gestionar
posiciones paper: no se inventan fills. Un hueco >60 s pausa nuevas entradas.

Ver ESSES_V1.md para reglas, límites, protocolo, pruebas y modo de ejecución local.
