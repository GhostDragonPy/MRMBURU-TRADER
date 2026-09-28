"""Public identity of the personal Discord assistant. No secrets live here."""

DISPLAY_NAME = 'GhostDragon'
DISPLAY_CHANNEL = '#tradehouse'
PERSONA = 'asistente personal'
MODE = 'paper'
EXECUTION_ENABLED = False
ACTIVITY = 'paper · Esses · GhostDragon'
READ_RECEIPT = '👀'
WORKING = '🔧'
CAT = '🐈'

ADMIN_COMMANDS = (
    'status', 'positions', 'history', 'daily_report',
    'pause', 'resume', 'paper_order', 'paper_close', 'broker_order',
    'demo_status', 'demo_emergency_stop', 'demo_preflight',
)
PERSONAL_COMMANDS = ('ayuda', 'identidad', 'clima', 'tokens', 'recordatorio', 'buscar')


def banner():
    return (
        f'{CAT} **{DISPLAY_NAME}** — {PERSONA} de MRMBURU ({DISPLAY_CHANNEL}).\n'
        f'Modo **{MODE}**. `execution_enabled={str(EXECUTION_ENABLED).lower()}`. '
        'Esses y `/paper_order` son paper. `/broker_order` es orden real mínima.'
    )


def help_text():
    admin = ', '.join(f'/{name}' for name in ADMIN_COMMANDS)
    personal = ', '.join(f'/{name}' for name in PERSONAL_COMMANDS)
    return (
        f'{banner()}\n\n'
        f'**Comandos personales:** {personal}\n'
        f'**Comandos administrativos paper:** {admin}\n\n'
        'Los comandos se autorizan por ID de guild, canal, usuario y rol, no por nombres. '
        'Pausa/reanudación solo afecta entradas automáticas paper. '
        '`/paper_order` y `/paper_close` usan la cuenta aislada `discord-sandbox`. '
        '`/demo_status`, `/demo_preflight` y `/demo_emergency_stop` cubren demo-orders. '
        '`/pause` detiene entradas nuevas paper y demo, no cierra posiciones.'
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
        'display_channel': DISPLAY_CHANNEL,
    }
