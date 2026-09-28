"""Public identity of the personal Discord assistant. No secrets live here."""

DISPLAY_NAME = 'GhostDragon'
PERSONA = 'asistente personal'
MODE = 'paper'
EXECUTION_ENABLED = False
ACTIVITY = 'paper · Esses · GhostDragon'
READ_RECEIPT = '👀'
WORKING = '🔧'
CAT = '🐈'

ADMIN_COMMANDS = (
    'status', 'positions', 'history', 'daily_report',
    'pause', 'resume', 'paper_order', 'paper_close',
)
PERSONAL_COMMANDS = ('ayuda', 'identidad', 'clima', 'tokens', 'recordatorio', 'buscar')


def banner():
    return (
        f'{CAT} **{DISPLAY_NAME}** — {PERSONA} de MRMBURU.\n'
        f'Modo **{MODE}**. `execution_enabled={str(EXECUTION_ENABLED).lower()}`. '
        'No envío órdenes a cTrader.'
    )


def help_text():
    admin = ', '.join(f'/{name}' for name in ADMIN_COMMANDS)
    personal = ', '.join(f'/{name}' for name in PERSONAL_COMMANDS)
    return (
        f'{banner()}\n\n'
        f'**Comandos personales:** {personal}\n'
        f'**Comandos administrativos paper:** {admin}\n\n'
        'Visible y ejecutable solo en #tradehouse por acfz. '
        'Pausa/reanudación solo afecta entradas automáticas paper. '
        '`/paper_order` y `/paper_close` usan la cuenta aislada `discord-sandbox`. '
        'Ningún comando envía órdenes reales ni muestra secretos.'
    )


def identity_payload():
    return {
        'name': DISPLAY_NAME,
        'persona': PERSONA,
        'mode': MODE,
        'execution_enabled': EXECUTION_ENABLED,
        'activity': ACTIVITY,
        'admin_commands': list(ADMIN_COMMANDS),
        'personal_commands': list(PERSONAL_COMMANDS),
        'orders': 'disabled',
        'channel': 'tradehouse',
        'operator': 'acfz',
    }
