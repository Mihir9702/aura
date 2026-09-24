"""Create the loopback-local Aura integration database if absent.

The name comes from AURA_TEST_DATABASE_NAME (default aura_test) and must match
aura_test(_[a-z0-9]+)?, so parallel worktrees can use separate databases on one cluster.
--fresh drops and recreates that database; nothing outside the pattern is ever touched.
Credentials stay out of argv and logs.
"""
import argparse
import os
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from aura.config import Settings

TEST_DATABASE_NAME = re.compile(r"aura_test(_[a-z0-9]+)?")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the Aura integration database.")
    parser.add_argument("--fresh", action="store_true",
                        help="drop and recreate the test database first")
    fresh = parser.parse_args().fresh
    name = os.environ.get("AURA_TEST_DATABASE_NAME") or "aura_test"
    if not TEST_DATABASE_NAME.fullmatch(name):
        raise SystemExit("AURA_TEST_DATABASE_NAME must match aura_test(_[a-z0-9]+)?")
    url = make_url(Settings().database_url.get_secret_value())
    if url.host != "127.0.0.1" or url.port != 55432 or url.database != name:
        raise SystemExit(f"Test setup requires the isolated loopback {name} database")
    engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT",
                           hide_parameters=True)
    try:
        with engine.connect() as connection:
            exists = connection.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"),
                                       {"name": name})
            # The name matched the pattern above, so it is safe as a quoted identifier.
            if fresh and exists:
                connection.execute(text(f'DROP DATABASE "{name}"'))
                print(f"Dropped test database {name}.")
                exists = None
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
                print(f"Created test database {name}.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
