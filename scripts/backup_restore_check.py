"""Verify a local backup using a newly created disposable restore database.

Never drops or overwrites an existing database. Credentials stay out of argv/logs.
The expected migration is Alembic's single head, so the drill works at any head.
"""
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from aura.config import Settings

root = Path(__file__).resolve().parent.parent
alembic_config = Config(str(root / "alembic.ini"))
location = alembic_config.get_main_option("script_location")
if not location:
    raise RuntimeError("alembic.ini defines no script_location")
# Resolve against the repository, not the working directory; escape ConfigParser '%'.
alembic_config.set_main_option("script_location", str(root / location).replace("%", "%%"))
# get_current_head() refuses multiple heads, so the expected revision is unambiguous.
head = ScriptDirectory.from_config(alembic_config).get_current_head()
if head is None:
    raise RuntimeError("Alembic reports no migration head")
url = make_url(Settings().database_url.get_secret_value())
if url.host != "127.0.0.1" or url.port != 55432 or url.database != "aura":
    raise RuntimeError("Restore drill supports only the isolated local Aura cluster")
bin_dir = Path(r"C:\Program Files\PostgreSQL\17\bin")
dump = Path(".cache/restore-check.dump").resolve()
dump.parent.mkdir(exist_ok=True)
env = dict(os.environ, PGPASSWORD=url.password or "")
args = ["-h", "127.0.0.1", "-p", "55432", "-U", url.username or "aura"]
subprocess.run([str(bin_dir / "pg_dump.exe"), *args, "-d", "aura", "-Fc", "-f", str(dump)],
               env=env, check=True, capture_output=True)
restore_name = "aura_restore_" + uuid4().hex
admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
created = False
try:
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{restore_name}"'))
        created = True
    subprocess.run([str(bin_dir / "pg_restore.exe"), *args, "-d", restore_name,
                    "--exit-on-error", str(dump)], env=env, check=True, capture_output=True)
    restored = create_engine(url.set(database=restore_name))
    try:
        with restored.connect() as connection:
            invalid = connection.scalar(text("SELECT count(*) FROM (SELECT journal_id FROM postings "
                "GROUP BY journal_id HAVING sum(amount) <> 0) invalid"))
            funding = connection.scalar(text("SELECT count(*) FROM journals "
                                              "WHERE source_key='fund:challenge'"))
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            if invalid or funding != 1:
                raise RuntimeError("Restored ledger verification failed")
            if version != head:
                raise RuntimeError(f"Restored migration {version} is not the Alembic head {head}")
        print(f"Restore verified: balanced journals, unique challenge funding, migration {head}.")
    finally:
        restored.dispose()
finally:
    if created:
        # This identifier was created by this invocation; no existing DB can be targeted.
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{restore_name}"'))
    admin.dispose()
