"""Small repository guard; not a substitute for a full secret scanner."""
from pathlib import Path
import re,subprocess
files=subprocess.check_output(['git','ls-files','-z']).decode().split('\0')
errors=[]
for name in filter(None,files):
    p=Path(name)
    if (p.name.startswith('.env') and p.name!='.env.example') or p.suffix in {'.pem','.key','.pfx','.p12'}:
        errors.append(name)
    if p.is_file():
        data=p.read_text(errors='ignore')
        if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data) or re.search(r'gh[pousr]_[A-Za-z0-9]{30,}',data):
            errors.append(name)
if errors:raise SystemExit('Forbidden files/secrets: '+', '.join(errors))
print('Repository secret guard passed')
