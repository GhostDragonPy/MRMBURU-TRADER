"""Creates local secrets; never prints them and never overwrites an existing .env."""
import os
import secrets
from pathlib import Path

path=Path(__file__).resolve().parents[1]/'.env'
content=('APP_ENV=development\nTRADING_MODE=paper\nEXECUTION_ENABLED=false\n'
         'POSTGRES_DB=mrmburu\nPOSTGRES_USER=mrmburu\nPOSTGRES_HOST=postgres\n'
         f'POSTGRES_PASSWORD={secrets.token_urlsafe(36)}\n'
         f'ADMIN_API_KEY={secrets.token_urlsafe(36)}\n'
         f'RESEARCH_API_KEY={secrets.token_urlsafe(36)}\n'
         'REDIS_URL=redis://redis:6379/0\n'
         'DEEPSEEK_API_KEY=\nFRED_API_KEY=\nCTRADER_CLIENT_ID=\nCTRADER_CLIENT_SECRET=\n'
         'CTRADER_REDIRECT_URI=https://trader.acshop.shop/research/ctrader/callback\n'
         'CTRADER_ACCESS_TOKEN=\nCTRADER_ACCOUNT_ID=\n'))
try:
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
except FileExistsError:
    raise SystemExit('.env already exists; preserved')
with os.fdopen(fd,'w') as f:f.write(content)
print('Created .env with owner-only permissions; secrets are not printed.')
