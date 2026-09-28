# Discord control (paper only)

The Discord process is the personal **GhostDragon** assistant plus MRMBURU
administrative slash commands. It cannot place, change, or close cTrader orders.
`EXECUTION_ENABLED` remains false. Personal commands (`/ayuda`, `/identidad`,
`/clima`, `/tokens`, `/recordatorio`, `/buscar`) keep the existing Spanish
identity. Administrative commands (`/status`, `/positions`, `/history`,
`/daily_report`, `/pause`, `/resume`, `/paper_order`, `/paper_close`) call
`/internal/discord/*`.

The container receives only its Discord token, an internal API key, IDs used for
authorization, and Redis/API addresses. It does not receive the cTrader
environment variables.

Never run two gateways with the same `DISCORD_BOT_TOKEN`. Stop nanobot (or any
other Discord client using that token) before enabling this service. Startup
takes a Redis lock (`discord:gateway:lock`) and exits if another process already
holds the same token.

## Discord application setup

1. In the Discord Developer Portal create an application and bot.
2. Keep public bot disabled. No privileged gateway intents are required.
3. Under OAuth2 URL Generator select `bot` and `applications.commands`. Grant only
   View Channels and Send Messages. Do not grant Administrator.
4. Invite it only to the configured guild.
5. Create or select an administrator role and collect the guild, role, and allowed user IDs.
6. Generate the bot token and a separate random internal API key of at least 32 characters.
   Store both only in the VPS `.env`; never paste them into Discord or commit them.

Variables are documented in `.env.example`. `DISCORD_ALLOWED_USER_IDS` is a
comma-separated allowlist. Guild, allowlisted user, and administrator role must all match.
The bot calls `/internal/discord/*` with `X-Discord-Api-Key`. The API rejects every
request to those routes when `DISCORD_API_KEY` is absent or incorrect, including requests
forwarded by Caddy. The bot healthcheck requires a fresh gateway-ready Redis heartbeat.

## Validation and deployment commands

The service is disabled unless both the Compose profile and `DISCORD_BOT_ENABLED=true`
are selected. Review and back up PostgreSQL before applying migrations.

    docker compose build api worker discord-bot
    docker compose run --rm migrate
    docker compose up -d api worker
    docker compose --profile discord up -d discord-bot
    docker compose ps
    docker compose logs --tail=100 discord-bot

To leave it disabled, keep `DISCORD_BOT_ENABLED=false` and do not select the
`discord` profile. No production deployment is performed by this change.
