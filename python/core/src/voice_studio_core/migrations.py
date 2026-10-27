"""Versioned, immutable schema DDL for SQLite and PostgreSQL.

Add new numbered migrations; never regenerate an existing migration after release.
A transaction plus database lock protects concurrent controller/worker startup.
"""

import json
from importlib.resources import files

from sqlalchemy import inspect, text


def migrate(engine):
    dialect = engine.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("Only SQLite and PostgreSQL are supported")
    with engine.begin() as connection:
        if dialect == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(8913472901)"))
        tables = inspect(connection).get_table_names()
        if "voice_schema" not in tables:
            if "voices" in tables:
                raise RuntimeError(
                    "Unversioned database detected; export data before upgrading this preview"
                )
            connection.exec_driver_sql("CREATE TABLE voice_schema (version INTEGER NOT NULL)")
            version = 0
        else:
            version = (
                connection.execute(text("SELECT version FROM voice_schema")).scalar_one_or_none()
                or 0
            )
        if version > 2:
            raise RuntimeError("Database was written by a newer release; do not downgrade it")
        if version == 0:
            migration = json.loads(
                files("voice_studio_core").joinpath("schema-0001.json").read_text()
            )
            for statement in migration[dialect]:
                connection.exec_driver_sql(statement)
            connection.execute(text("INSERT INTO voice_schema (version) VALUES (1)"))

        if version < 2:
            migration = json.loads(files("voice_studio_core").joinpath("schema-0002.json").read_text())
            for statement in migration[dialect]:
                connection.exec_driver_sql(statement)
            connection.exec_driver_sql("UPDATE voice_schema SET version = 2")
