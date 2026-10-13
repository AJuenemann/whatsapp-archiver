import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from .models import Base


def create_database(database_url: str | None = None):
    url = database_url or os.getenv("DATABASE_URL", "sqlite:////data/archive.db")
    if url.startswith("sqlite:"):
        database_path = url.removeprefix("sqlite://")
        if database_path != "/:memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    else:
        engine = create_engine(url)

    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if "messages" in tables:
                columns = {column["name"] for column in inspect(connection).get_columns("messages")}
                for name, sql_type in (
                    ("source_key", "VARCHAR(255)"),
                    ("raw_data", "TEXT"),
                    ("deleted_at", "DATETIME"),
                ):
                    if name not in columns:
                        connection.exec_driver_sql(f"ALTER TABLE messages ADD COLUMN {name} {sql_type}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)