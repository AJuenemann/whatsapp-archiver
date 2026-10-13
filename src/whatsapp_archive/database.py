import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from .models import Base


def create_database(database_url: str | None = None):
    url = database_url or os.getenv("DATABASE_URL", "sqlite:////data/archive.db")
    identity_path = None
    parsed_url = make_url(url)
    if parsed_url.get_backend_name() == "sqlite":
        database_path = parsed_url.database
        if database_path and database_path != ":memory:":
            path = Path(database_path)
            identity_path = path.with_name(path.name + ".identity")
            if identity_path.exists() and not path.is_file():
                raise RuntimeError("Existing archive database is missing; refusing to create an empty archive")
            path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA recursive_triggers=ON")
    else:
        engine = create_engine(url)

    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if identity_path is not None and identity_path.exists():
                archive_id = (
                    connection.exec_driver_sql(
                        "SELECT value FROM archive_metadata WHERE key = 'archive_id'"
                    ).scalar() if "archive_metadata" in tables else None
                )
                if archive_id != identity_path.read_text("ascii").strip():
                    raise RuntimeError("Archive identity mismatch; refusing to use a different database")
            if "messages" in tables:
                columns = {column["name"] for column in inspect(connection).get_columns("messages")}
                for name, sql_type in (
                    ("source_key", "VARCHAR(255)"),
                    ("raw_data", "TEXT"),
                    ("deleted_at", "DATETIME"),
                ):
                    if name not in columns:
                        connection.exec_driver_sql(f"ALTER TABLE messages ADD COLUMN {name} {sql_type}")
            if "chats" in tables:
                columns = {column["name"] for column in inspect(connection).get_columns("chats")}
                if "alias" not in columns:
                    connection.exec_driver_sql("ALTER TABLE chats ADD COLUMN alias VARCHAR(500)")
            if "message_versions" in tables:
                columns = {column["name"] for column in inspect(connection).get_columns("message_versions")}
                for name, sql_type in (("sender", "VARCHAR(500)"), ("sent_at", "DATETIME")):
                    if name not in columns:
                        connection.exec_driver_sql(f"ALTER TABLE message_versions ADD COLUMN {name} {sql_type}")
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            archive_id = connection.exec_driver_sql(
                "SELECT value FROM archive_metadata WHERE key = 'archive_id'"
            ).scalar()
            if identity_path is not None and identity_path.exists():
                expected = identity_path.read_text("ascii").strip()
                if not archive_id or archive_id != expected:
                    raise RuntimeError("Archive identity mismatch; refusing to use a different database")
            if not archive_id:
                archive_id = str(uuid.uuid4())
                connection.exec_driver_sql(
                    "INSERT INTO archive_metadata (key, value) VALUES ('archive_id', ?)", (archive_id,)
                )
            for table in ("chats", "messages", "message_versions", "media", "contact_mappings", "archive_metadata"):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS protect_{table}_delete BEFORE DELETE ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'Archive records cannot be deleted'); END"
                )
            for table in ("message_versions", "media", "contact_mappings", "archive_metadata"):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS protect_{table}_update BEFORE UPDATE ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'Archived records are immutable'); END"
                )
            connection.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS protect_message_identity BEFORE UPDATE ON messages "
                "WHEN OLD.id IS NOT NEW.id OR OLD.chat_id IS NOT NEW.chat_id "
                "OR OLD.identity_key IS NOT NEW.identity_key OR OLD.source_key IS NOT NEW.source_key "
                "BEGIN SELECT RAISE(ABORT, 'Archived message identity is immutable'); END"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS protect_chat_identity BEFORE UPDATE ON chats "
                "WHEN OLD.id IS NOT NEW.id OR OLD.identity_key IS NOT NEW.identity_key "
                "BEGIN SELECT RAISE(ABORT, 'Archived chat identity is immutable'); END"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS preserve_message_content BEFORE UPDATE ON messages "
                "WHEN OLD.text IS NOT NEW.text OR OLD.raw_data IS NOT NEW.raw_data "
                "OR OLD.sender IS NOT NEW.sender OR OLD.sent_at IS NOT NEW.sent_at "
                "BEGIN INSERT INTO message_versions (message_id, text, raw_data, sender, sent_at, observed_at) "
                "VALUES (OLD.id, OLD.text, OLD.raw_data, OLD.sender, OLD.sent_at, CURRENT_TIMESTAMP); END"
            )
        if identity_path is not None and not identity_path.exists():
            with identity_path.open("x", encoding="ascii") as marker:
                marker.write(archive_id + "\n")
                marker.flush()
                os.fsync(marker.fileno())
    return engine, sessionmaker(bind=engine, expire_on_commit=False)