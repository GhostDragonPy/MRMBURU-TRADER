"""Personal Discord bot plus MRMBURU paper slash commands. No cTrader credentials."""
import asyncio, json, logging, os
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import discord
from discord import app_commands
from redis import Redis
from apps.discord_bot import gateway_lock, identity, personal
from apps.discord_bot.security import (
    AuthorizationError, MAX_POSITION_ID, MAX_REASON, allowed, bounded, rate_limit,
)

logging.basicConfig(level=logging.INFO)
INTERNAL_PATHS = frozenset({
    '/internal/discord/status', '/internal/discord/positions',
    '/internal/discord/history', '/internal/discord/daily-report',
    '/internal/discord/paper-order', '/internal/discord/paper-close',
    '/internal/discord/broker-order',
    '/internal/discord/pause', '/internal/discord/resume',
})

@dataclass(frozen=True)
class BotSettings:
    enabled: bool
    token: str
    api_key: str
    guild_id: int
    admin_role_id: int
    channel_id: int
    allowed_user_ids: str
    api_url: str
    redis_url: str
    rate_limit: int

def load_settings():
    return BotSettings(os.getenv('DISCORD_BOT_ENABLED','false').lower() == 'true',
        os.getenv('DISCORD_BOT_TOKEN',''), os.getenv('DISCORD_API_KEY',''),
        int(os.getenv('DISCORD_GUILD_ID','0')), int(os.getenv('DISCORD_ADMIN_ROLE_ID','0')),
        int(os.getenv('DISCORD_CHANNEL_ID','0')),
        os.getenv('DISCORD_ALLOWED_USER_IDS',''), os.getenv('DISCORD_API_URL','http://api:8000'),
        os.getenv('REDIS_URL','redis://redis:6379/0'),
        int(os.getenv('DISCORD_RATE_LIMIT_PER_MINUTE','10')))

settings = load_settings()
cache = Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
lock = None

def api(path, user_id, method='GET', payload=None, timeout=10):
    if path not in INTERNAL_PATHS:
        raise RuntimeError('Internal path not allowed')
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(settings.api_url.rstrip('/')+path, data=data, method=method,
        headers={'content-type':'application/json',
                 'x-discord-api-key':settings.api_key,
                 'x-discord-user-id':str(user_id)})
    try:
        with urlopen(request, timeout=timeout) as response: return json.loads(response.read())
    except HTTPError as exc:
        raise RuntimeError(f'Internal API rejected request ({exc.code})') from None

async def call(path, interaction, method='GET', payload=None, timeout=10):
    return await asyncio.to_thread(api, path, interaction.user.id, method, payload, timeout)

def authorize(interaction):
    roles = [role.id for role in getattr(interaction.user, 'roles', ())]
    return allowed(
        guild_id=interaction.guild_id, user_id=interaction.user.id, role_ids=roles,
        channel_id=interaction.channel_id,
        expected_guild_id=settings.guild_id, expected_channel_id=settings.channel_id,
        admin_role_id=settings.admin_role_id,
        allowed_user_ids=settings.allowed_user_ids,
    )

class Confirm(discord.ui.View):
    def __init__(self, owner_id, action):
        super().__init__(timeout=60); self.owner_id=owner_id; self.action=action
    @discord.ui.button(label='Confirmar', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('Confirmación no autorizada.', ephemeral=True); return
        if not await guard(interaction): return
        for item in self.children: item.disabled=True
        await interaction.response.defer(ephemeral=True)
        try: result=await self.action(interaction)
        except Exception as exc:
            await interaction.edit_original_response(content=f'Rechazado: {exc}',view=self); return
        await interaction.edit_original_response(content=json.dumps(result,indent=2)[:1900],view=self)
    @discord.ui.button(label='Cancelar', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('Cancelación no autorizada.',ephemeral=True); return
        for item in self.children: item.disabled=True
        await interaction.response.edit_message(content='Cancelado.',view=self)

class Client(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True)); self.tree=app_commands.CommandTree(self)
    async def setup_hook(self):
        guild=discord.Object(id=settings.guild_id)
        self.tree.copy_global_to(guild=guild); await self.tree.sync(guild=guild)
        self._health_task = asyncio.create_task(self.health_loop())

    async def on_ready(self):
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name=identity.ACTIVITY))

    async def health_loop(self):
        while not self.is_closed():
            if self.is_ready() and lock is not None:
                try:
                    await asyncio.to_thread(cache.setex, 'discord:bot:healthy', 45, '1')
                    await asyncio.to_thread(lock.refresh, cache)
                except Exception:
                    await self.close(); return
                await self.flush_reminders()
            await asyncio.sleep(15)

    async def flush_reminders(self):
        due = await asyncio.to_thread(personal.due_reminders, cache)
        for item in due:
            channel = self.get_channel(int(item['channel_id']))
            if channel is None or int(item['channel_id']) != settings.channel_id:
                continue
            mention = f"<@{item['user_id']}>"
            await channel.send(f"{identity.CAT} Recordatorio {mention}: {item['text']}")

    async def close(self):
        if lock is not None:
            await asyncio.to_thread(lock.release, cache)
        await super().close()

client=Client()

async def guard(interaction):
    try:
        authorize(interaction)
        rate_limit(cache, user_id=interaction.user.id, limit=settings.rate_limit)
        return True
    except (AuthorizationError, RuntimeError, ValueError) as exc:
        if interaction.response.is_done(): await interaction.followup.send(str(exc),ephemeral=True)
        else: await interaction.response.send_message(str(exc),ephemeral=True)
        return False

def render(data): return json.dumps(data,indent=2)[:1900]

@client.tree.command(name='ayuda',description='Identidad GhostDragon y comandos disponibles')
async def ayuda(interaction):
    if not await guard(interaction): return
    personal.record_usage(cache, interaction.user.id, 'ayuda')
    await interaction.response.send_message(identity.help_text(), ephemeral=True)

@client.tree.command(name='identidad',description='Quién es este bot y qué no puede hacer')
async def identidad(interaction):
    if not await guard(interaction): return
    personal.record_usage(cache, interaction.user.id, 'identidad')
    await interaction.response.send_message(render(identity.identity_payload()), ephemeral=True)

@client.tree.command(name='clima',description='Consulta el clima (HTTPS wttr.in)')
async def clima(interaction, ciudad:str):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        text = await asyncio.to_thread(personal.weather, ciudad)
    except (ValueError, RuntimeError) as exc:
        await interaction.followup.send(str(exc), ephemeral=True); return
    personal.record_usage(cache, interaction.user.id, 'clima')
    await interaction.followup.send(text, ephemeral=True)

@client.tree.command(name='tokens',description='Estadísticas de uso de comandos, sin secretos')
async def tokens(interaction):
    if not await guard(interaction): return
    personal.record_usage(cache, interaction.user.id, 'tokens')
    report = personal.usage_report(cache, interaction.user.id)
    await interaction.response.send_message(render(report), ephemeral=True)

@client.tree.command(name='recordatorio',description='Aviso paper en el canal autorizado (America/Asuncion)')
async def recordatorio(interaction, minutos:int, texto:str):
    if not await guard(interaction): return
    try:
        saved = personal.schedule_reminder(
            cache, user_id=interaction.user.id, channel_id=interaction.channel_id,
            minutes=minutos, text=texto)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True); return
    personal.record_usage(cache, interaction.user.id, 'recordatorio')
    await interaction.response.send_message(
        f"{identity.CAT} Listo ({saved['timezone']}) {saved['due']}: {saved['text']}", ephemeral=True)

@client.tree.command(name='buscar',description='Búsqueda breve, sin URLs ni shell')
async def buscar(interaction, consulta:str):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        text = await asyncio.to_thread(personal.search, consulta)
    except (ValueError, RuntimeError) as exc:
        await interaction.followup.send(str(exc), ephemeral=True); return
    personal.record_usage(cache, interaction.user.id, 'buscar')
    await interaction.followup.send(text, ephemeral=True)

@client.tree.command(name='status',description='Estado del bot paper')
async def status(interaction):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    payload = await call('/internal/discord/status',interaction)
    payload['identity'] = identity.DISPLAY_NAME
    await interaction.followup.send(identity.banner()+'\n'+render(payload),ephemeral=True)

@client.tree.command(name='positions',description='Posiciones manuales paper abiertas')
async def positions(interaction):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(render(await call('/internal/discord/positions',interaction)),ephemeral=True)

@client.tree.command(name='history',description='Operaciones manuales recientes')
async def history(interaction):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(render(await call('/internal/discord/history',interaction)),ephemeral=True)

@client.tree.command(name='daily_report',description='Informe de discord-sandbox')
async def daily_report(interaction):
    if not await guard(interaction): return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(render(await call('/internal/discord/daily-report',interaction)),ephemeral=True)

async def control_prompt(interaction, action, reason):
    if not await guard(interaction): return
    try:
        reason = bounded(reason, minimum=3, maximum=MAX_REASON, field='motivo')
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True); return
    payload={'interaction_id':str(interaction.id),'reason':reason}
    async def execute(confirm_interaction):
        return await call('/internal/discord/'+action,confirm_interaction,'POST',payload)
    await interaction.response.send_message(f'Confirmar /{action}: {reason}',
        view=Confirm(interaction.user.id,execute),ephemeral=True)

@client.tree.command(name='pause',description='Pausar nuevas entradas automáticas paper')
async def pause(interaction, reason:str): await control_prompt(interaction,'pause',reason)

@client.tree.command(name='resume',description='Reanudar entradas automáticas paper')
async def resume(interaction, reason:str): await control_prompt(interaction,'resume',reason)

@client.tree.command(name='paper_order',description='Crear una operación manual simulada EURUSD')
@app_commands.describe(side='buy o sell',stop_loss='Stop loss',take_profit='Take profit',
                       risk_percent='Porcentaje (máximo 0.25)',reason='Motivo obligatorio')
@app_commands.choices(side=[app_commands.Choice(name='Buy',value='buy'),app_commands.Choice(name='Sell',value='sell')])
async def paper_order(interaction, side:app_commands.Choice[str], stop_loss:float,
                      take_profit:float, risk_percent:float, reason:str):
    if not await guard(interaction): return
    if risk_percent <= 0 or risk_percent > .25:
        await interaction.response.send_message('El riesgo debe estar entre 0 y 0.25%.',ephemeral=True); return
    try:
        reason = bounded(reason, minimum=3, maximum=MAX_REASON, field='motivo')
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True); return
    payload={'interaction_id':str(interaction.id),'side':side.value,'stop_loss':str(stop_loss),
        'take_profit':str(take_profit),'risk_percent':str(risk_percent),'reason':reason}
    async def execute(confirm_interaction):
        return await call('/internal/discord/paper-order',confirm_interaction,'POST',payload)
    summary=f'EURUSD {side.value.upper()} market | SL {stop_loss} | TP {take_profit} | riesgo {risk_percent}% | {reason}'
    await interaction.response.send_message(summary+'\n¿Confirmar simulación?',
        view=Confirm(interaction.user.id,execute),ephemeral=True)

@client.tree.command(name='broker_order',description='Enviar 1 volumen mínimo EURUSD a cTrader (verificación)')
@app_commands.describe(side='buy o sell',reason='Motivo obligatorio')
@app_commands.choices(side=[app_commands.Choice(name='Buy',value='buy'),app_commands.Choice(name='Sell',value='sell')])
async def broker_order(interaction, side:app_commands.Choice[str], reason:str):
    if not await guard(interaction): return
    try:
        reason = bounded(reason, minimum=3, maximum=MAX_REASON, field='motivo')
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True); return
    payload={'interaction_id':str(interaction.id),'side':side.value,'reason':reason}
    async def execute(confirm_interaction):
        return await call('/internal/discord/broker-order',confirm_interaction,'POST',payload,35)
    await interaction.response.send_message(
        f'ORDEN REAL cTrader EURUSD {side.value.upper()} volumen mínimo | {reason}\n¿Confirmar?',
        view=Confirm(interaction.user.id,execute),ephemeral=True)

@client.tree.command(name='paper_close',description='Cerrar una posición manual simulada')
async def paper_close(interaction, position_id:str, reason:str):
    if not await guard(interaction): return
    try:
        position_id = bounded(position_id, minimum=1, maximum=MAX_POSITION_ID, field='position_id')
        reason = bounded(reason, minimum=3, maximum=MAX_REASON, field='motivo')
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True); return
    payload={'interaction_id':str(interaction.id),'position_id':position_id,'reason':reason}
    async def execute(confirm_interaction):
        return await call('/internal/discord/paper-close',confirm_interaction,'POST',payload)
    await interaction.response.send_message(f'Cerrar {position_id}: {reason}\n¿Confirmar?',
        view=Confirm(interaction.user.id,execute),ephemeral=True)

def main():
    global lock
    if not settings.enabled: raise SystemExit('Discord bot is disabled')
    if not all((settings.token,settings.api_key,settings.guild_id,settings.admin_role_id,
                settings.channel_id,settings.allowed_user_ids)):
        raise SystemExit('Discord bot configuration is incomplete')
    lock = gateway_lock.GatewayLock(settings.token)
    lock.acquire(cache)
    client.run(settings.token, log_handler=None)

if __name__ == '__main__': main()
