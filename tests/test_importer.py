import tempfile
import unittest
import zipfile
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select

from whatsapp_archive.database import create_database
from whatsapp_archive.importer import import_export, ingest_whatsapp_event, scan_whatsapp_events
from whatsapp_archive.models import Chat, Media, Message, MessageVersion


class ImportExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.engine, self.session_factory = create_database(f"sqlite:///{self.root / 'archive.db'}")
        self.media_root = self.root / "media"

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_overlapping_text_exports_only_add_new_messages(self):
        first = self.root / "first.txt"
        first.write_text("31.12.23, 22:15 - Ada: One\n31.12.23, 22:16 - Bob: Two", encoding="utf-8")
        second = self.root / "second.txt"
        second.write_text(
            "31.12.23, 22:15 - Ada: One\n31.12.23, 22:16 - Bob: Two\n31.12.23, 22:17 - Ada: Three",
            encoding="utf-8",
        )

        first_result = import_export(first, "Test chat", "day-first", self.session_factory, self.media_root)
        second_result = import_export(second, "Test chat", "day-first", self.session_factory, self.media_root)

        self.assertEqual(first_result.messages_added, 2)
        self.assertEqual(second_result.messages_added, 1)
        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count(Message.id))), 3)

    def test_zip_import_archives_referenced_media_and_reimport_is_idempotent(self):
        source = self.root / "export.zip"
        media_name = "IMG-20261201-WA0001.jpg"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("WhatsApp Chat.txt", f"01.12.26, 12:00 - Ada: {media_name} (file attached)")
            archive.writestr(f"media/{media_name}", b"test image bytes")

        first = import_export(source, "Photos", "day-first", self.session_factory, self.media_root)
        repeated = import_export(source, "Photos", "day-first", self.session_factory, self.media_root)

        self.assertEqual(first.media_added, 1)
        self.assertEqual(repeated.status, "already-imported")
        with self.session_factory() as session:
            media = session.scalar(select(Media))
            self.assertTrue((self.media_root / media.stored_path).is_file())

    def test_zip_path_traversal_is_rejected(self):
        source = self.root / "unsafe.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("../outside.txt", "31.12.23, 22:15 - Ada: Nope")
            archive.writestr("WhatsApp Chat.txt", "31.12.23, 22:15 - Ada: Safe")

        with self.assertRaisesRegex(ValueError, "unsafe path"):
            import_export(source, "Unsafe", "day-first", self.session_factory, self.media_root)

        self.assertFalse((self.root.parent / "outside.txt").exists())

    def test_same_export_with_changed_date_order_is_a_new_interpretation(self):
        source = self.root / "ambiguous-date.txt"
        source.write_text("01/02/2024, 09:15 - Ada: Hello", encoding="utf-8")

        first = import_export(source, "Dates", "day-first", self.session_factory, self.media_root)
        repeated = import_export(source, "Dates", "day-first", self.session_factory, self.media_root)
        alternate = import_export(source, "Dates", "month-first", self.session_factory, self.media_root)

        self.assertEqual(first.messages_added, 1)
        self.assertEqual(repeated.status, "already-imported")
        self.assertEqual(alternate.status, "complete")
        self.assertEqual(alternate.messages_added, 1)
        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count(Message.id))), 2)

    def test_incoming_scanner_retries_after_metadata_changes(self):
        from unittest.mock import patch

        from whatsapp_archive import importer

        incoming = self.root / "incoming"
        incoming.mkdir()
        source = incoming / "scan.txt"
        source.write_text("31.12.23, 22:15 - Ada: Hello", encoding="utf-8")
        metadata = source.with_suffix(".txt.json")
        metadata.write_text('{"title":"Chat"}', encoding="utf-8")
        importer._SCAN_STATE.clear()

        with patch("whatsapp_archive.importer.import_export", side_effect=[OSError("temporary"), None]) as mocked:
            importer.scan_incoming(incoming, self.session_factory, self.media_root)
            importer.scan_incoming(incoming, self.session_factory, self.media_root)
            importer.scan_incoming(incoming, self.session_factory, self.media_root)
            self.assertEqual(mocked.call_count, 1)

            metadata.write_text('{"title":"Renamed chat"}', encoding="utf-8")
            importer.scan_incoming(incoming, self.session_factory, self.media_root)
            importer.scan_incoming(incoming, self.session_factory, self.media_root)

        self.assertEqual(mocked.call_count, 2)

    def test_incoming_scanner_retries_after_backoff_expires(self):
        from unittest.mock import patch

        from whatsapp_archive import importer

        incoming = self.root / "retry-incoming"
        incoming.mkdir()
        source = incoming / "retry.txt"
        source.write_text("31.12.23, 22:15 - Ada: Hello", encoding="utf-8")
        importer._SCAN_STATE.clear()

        with (
            patch("whatsapp_archive.importer.time.monotonic", side_effect=[100, 100, 100, 131]),
            patch("whatsapp_archive.importer.import_export", side_effect=[OSError("temporary"), None]) as mocked,
        ):
            importer.scan_incoming(incoming, self.session_factory, self.media_root)
            importer.scan_incoming(incoming, self.session_factory, self.media_root)
            importer.scan_incoming(incoming, self.session_factory, self.media_root)
            self.assertEqual(mocked.call_count, 1)
            importer.scan_incoming(incoming, self.session_factory, self.media_root)

        self.assertEqual(mocked.call_count, 2)

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

    def test_whatsapp_chat_archive_status_updates_without_erasing_known_state(self):
        ingest_whatsapp_event(
            {"type": "chat", "jid": "archive-status@g.us", "title": "Archive status", "isArchived": True},
            self.session_factory,
            self.media_root,
        )
        ingest_whatsapp_event(
            {"type": "chat", "jid": "archive-status@g.us", "title": "Archive status", "isArchived": False},
            self.session_factory,
            self.media_root,
        )
        ingest_whatsapp_event(
            {"type": "chat", "jid": "archive-status@g.us", "title": "Archive status"},
            self.session_factory,
            self.media_root,
        )

        with self.session_factory() as session:
            chat = session.scalar(select(Chat))
            self.assertFalse(chat.is_archived)

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

        scan_whatsapp_events(events_root, self.session_factory, self.media_root)

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