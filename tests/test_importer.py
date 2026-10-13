import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select

from whatsapp_archive.database import create_database
from whatsapp_archive.importer import ingest_whatsapp_event, scan_whatsapp_events
from whatsapp_archive.models import Chat, Message, MessageVersion


class LiveEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.engine, self.session_factory = create_database(f"sqlite:///{self.root / 'archive.db'}")
        self.media_root = self.root / "media"

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_whatsapp_history_live_replay_edit_and_delete_are_preserved(self):
        chat = {"type": "chat", "jid": "12345@g.us", "title": "Archive group"}
        message = {
            "type": "message",
            "jid": "12345@g.us",
            "title": "Archive group",
            "sourceId": "message-1:in:participant-1",
            "timestamp": "2025-12-31T22:15:00Z",
            "sender": "Ada",
            "text": "Original message",
        }
        ingest_whatsapp_event(chat, self.session_factory, self.media_root)
        ingest_whatsapp_event(message, self.session_factory, self.media_root)
        ingest_whatsapp_event(message, self.session_factory, self.media_root)
        ingest_whatsapp_event({**message, "text": "Edited message"}, self.session_factory, self.media_root)
        deleted = {"type": "delete", "jid": "12345@g.us", "sourceId": message["sourceId"]}
        ingest_whatsapp_event(deleted, self.session_factory, self.media_root)
        ingest_whatsapp_event(deleted, self.session_factory, self.media_root)

        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count(Chat.id))), 1)
            self.assertEqual(session.scalar(select(func.count(Message.id))), 1)
            stored = session.scalar(select(Message))
            self.assertEqual(stored.text, "Edited message")
            self.assertIsNotNone(stored.deleted_at)
            versions = session.scalars(select(MessageVersion).where(MessageVersion.message_id == stored.id)).all()
            self.assertEqual([version.text for version in versions], ["Original message", "Edited message"])

    def test_delete_before_message_creates_tombstone_and_keeps_it_deleted(self):
        delete_event = {
            "type": "delete",
            "jid": "late@g.us",
            "title": "Late chat",
            "sourceId": "late-message:in:participant",
        }
        message_event = {
            "type": "message",
            "jid": "late@g.us",
            "title": "Late chat",
            "sourceId": "late-message:in:participant",
            "timestamp": "2025-12-31T22:15:00Z",
            "sender": "Ada",
            "text": "Message arrived after its delete event",
        }

        ingest_whatsapp_event(delete_event, self.session_factory, self.media_root)
        ingest_whatsapp_event(message_event, self.session_factory, self.media_root)

        with self.session_factory() as session:
            message = session.scalar(select(Message))
            self.assertEqual(message.text, "Message arrived after its delete event")
            self.assertIsNotNone(message.deleted_at)
            self.assertEqual(session.scalar(select(func.count(MessageVersion.id))), 0)

    def test_whatsapp_event_spool_is_removed_after_import(self):
        events_root = self.root / "events"
        events_root.mkdir()
        event_path = events_root / "event-1.json"
        fixture = Path(__file__).parent / "fixtures" / "bridge-event.json"
        event_path.write_bytes(fixture.read_bytes())

        self.assertEqual(scan_whatsapp_events(events_root, self.session_factory, self.media_root), 1)

        self.assertFalse(event_path.exists())
        with self.session_factory() as session:
            message = session.scalar(select(Message))
            self.assertEqual(message.text, "Bridge event imported through the archive queue")

    def test_existing_sqlite_schema_gains_whatsapp_message_columns(self):
        legacy_path = self.root / "legacy.db"
        legacy_engine = create_engine(f"sqlite:///{legacy_path}")
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE chats (id INTEGER PRIMARY KEY, identity_key VARCHAR(64), "
                "title VARCHAR(500), created_at DATETIME)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id INTEGER, "
                "identity_key VARCHAR(64), sent_at DATETIME, sender VARCHAR(500), text TEXT)"
            )
        legacy_engine.dispose()

        upgraded, _session = create_database(f"sqlite:///{legacy_path}")
        columns = {column["name"] for column in inspect(upgraded).get_columns("messages")}

        self.assertTrue({"source_key", "raw_data", "deleted_at"}.issubset(columns))
        self.assertIn("message_versions", inspect(upgraded).get_table_names())
        upgraded.dispose()


if __name__ == "__main__":
    unittest.main()