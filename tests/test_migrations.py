from pathlib import Path
import os
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine,text,inspect
import pytest


def migrate(url):
    c=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
    c.attributes['connection_url']=url
    command.upgrade(c,'head')
    command.upgrade(c,'head')
    e=create_engine(url)
    with e.connect() as conn:
        assert conn.scalar(text('SELECT active FROM kill_switch WHERE id=1'))
        assert len(inspect(conn).get_table_names())==15
    command.check(c)
    command.downgrade(c,'base')
    command.upgrade(c,'head')
    e.dispose()

def test_sqlite_migration(tmp_path):migrate('sqlite:///'+str(tmp_path/'test.db'))

@pytest.mark.skipif(not os.environ.get('TEST_POSTGRES_URL'),reason='PostgreSQL integration runs in CI')
def test_postgres_migration():migrate(os.environ['TEST_POSTGRES_URL'])
