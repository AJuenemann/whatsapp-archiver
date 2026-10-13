import base64
import hashlib
import importlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from whatsapp_archive.database import create_database
from whatsapp_archive.importer import ingest_whatsapp_event
from whatsapp_archive.models import Chat, ContactMapping, Media, Message, MessageVersion
from whatsapp_archive.protection import create_snapshot, restore_snapshot, verify_snapshot


@pytest.fixture
def archive(tmp_path):
    engine, sessions = create_database(f"sqlite:///{tmp_path / 'archive.db'}")
    media_root, events_root = tmp_path / "media", tmp_path / "whatsapp-events"
    media_root.mkdir()
    events_root.mkdir()
    payload = b"Already archived attachment"
    (media_root / "attachment.bin").write_bytes(payload)
    original = {
        "type": "message", "jid": "123@g.us", "title": "Old chat",
        "sourceId": "old-message:in:Ada", "timestamp": "2025-01-01T00:00:00Z",
        "sender": "Ada", "text": "Original archived text",
        "rawData": {"message": {"conversation": "Original archived text"}},
        "media": {"path": "attachment.bin", "sha256": hashlib.sha256(payload).hexdigest()},
    }
    ingest_whatsapp_event(original, sessions, media_root)
    ingest_whatsapp_event(
        {"type": "contact_mapping", "lid": "123@lid", "phoneJid": "456@s.whatsapp.net"},
        sessions, media_root,
    )
    yield engine, sessions, media_root, events_root, original
    engine.dispose()


def test_empty_partial_and_conflicting_history_keeps_all_saved_content(archive, tmp_path):
    engine, sessions, media, events, original = archive
    untouched = {**original, "jid": "456@g.us", "sourceId": "untouched", "title": "Absent on new phone",
                 "text": "A completely absent chat"}
    ingest_whatsapp_event(untouched, sessions, media)
    before = create_snapshot(engine, media, events, tmp_path / "backups")
    # An empty history produces no events; reopening models a disconnected/relinked archive.
    restarted, restarted_sessions = create_database(str(engine.url))
    try:
        with restarted_sessions() as session:
            assert session.scalar(select(Message)).text == original["text"]
        ingest_whatsapp_event(
            {**original, "text": "", "rawData": {"message": {}}}, restarted_sessions, media
        )
        ingest_whatsapp_event(
            {**original, "sourceId": "new-message", "text": "New phone message"}, restarted_sessions, media
        )
        ingest_whatsapp_event(
            {"type": "delete", "jid": original["jid"], "sourceId": original["sourceId"]},
            restarted_sessions, media,
        )
        with restarted_sessions() as session:
            old = session.scalar(select(Message).where(Message.source_key == original["sourceId"]))
            versions = session.scalars(select(MessageVersion).where(MessageVersion.message_id == old.id)).all()
            assert any(version.text == original["text"] and json.loads(version.raw_data) == original["rawData"]
                       for version in versions)
            assert session.scalar(select(Media)).sha256 == original["media"]["sha256"]
            assert session.get(ContactMapping, "123@lid").phone_jid == "456@s.whatsapp.net"
            assert len(session.scalars(select(Message)).all()) == 3
            assert session.scalar(select(Message).where(Message.source_key == "untouched")).text == untouched["text"]
            assert len(session.scalars(select(Chat)).all()) == 2
        assert (media / "attachment.bin").read_bytes() == b"Already archived attachment"
        restore_snapshot(before, tmp_path / "restored")
        restored, restored_sessions = create_database(f"sqlite:///{tmp_path / 'restored' / 'archive.db'}")
        try:
            with restored_sessions() as session:
                assert session.scalar(select(Message)).text == original["text"]
        finally:
            restored.dispose()
    finally:
        restarted.dispose()


@pytest.mark.parametrize("filename", ["archive.db", "archive.db.identity", "whatsapp-events/pending.json"])
def test_corrupted_snapshot_is_rejected_before_restoration(archive, tmp_path, filename):
    engine, _sessions, media, events, _original = archive
    (events / "pending.json").write_text("{}", "ascii")
    snapshot = create_snapshot(engine, media, events, tmp_path / "backups")
    (snapshot / filename).write_bytes(b"Corrupted")
    with pytest.raises(ValueError):
        restore_snapshot(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("table", ["chats", "messages", "message_versions", "media", "contact_mappings", "archive_metadata"])
def test_physical_record_deletion_is_blocked(archive, table):
    engine, sessions, media, _events, original = archive
    ingest_whatsapp_event({**original, "text": "Edited"}, sessions, media)
    with pytest.raises(IntegrityError, match="cannot be deleted"):
        with engine.begin() as connection:
            connection.execute(text(f"DELETE FROM {table}"))


def test_real_message_with_deleted_label_is_versioned(archive):
    _engine, sessions, media, _events, original = archive
    ingest_whatsapp_event({**original, "text": "[Nachricht gelöscht]"}, sessions, media)
    ingest_whatsapp_event({**original, "text": "Replacement"}, sessions, media)
    with sessions() as session:
        assert "[Nachricht gelöscht]" in session.scalars(select(MessageVersion.text)).all()


def test_historical_replay_cannot_replace_existing_content(archive):
    _engine, sessions, media, _events, original = archive
    ingest_whatsapp_event(
        {**original, "historical": True, "text": "", "rawData": {"message": {"secretEncryptedMessage": {}}}},
        sessions, media,
    )
    with sessions() as session:
        saved = session.scalar(select(Message))
        assert saved.text == original["text"]
        assert json.loads(saved.raw_data) == original["rawData"]
        assert session.scalar(select(MessageVersion)) is None


def test_sql_update_preserves_text_raw_data_sender_and_time(archive):
    engine, sessions, _media, _events, original = archive
    with engine.begin() as connection:
        connection.execute(text("UPDATE messages SET text='Changed', raw_data=NULL, sender='Other'"))
    with sessions() as session:
        version = session.scalar(select(MessageVersion))
        assert version.text == original["text"]
        assert json.loads(version.raw_data) == original["rawData"]
        assert version.sender == "Ada"
        assert version.sent_at.isoformat() == "2025-01-01T00:00:00"
    for statement in ("UPDATE media SET stored_path='other'", "UPDATE message_versions SET text='Lost'",
                      "UPDATE messages SET source_key='other'", "UPDATE chats SET identity_key='other'",
                      "UPDATE contact_mappings SET phone_jid='999@s.whatsapp.net'",
                      "INSERT OR REPLACE INTO chats (id, identity_key, title, created_at) "
                      "SELECT id, identity_key, 'Replaced', created_at FROM chats"):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(statement))


def test_missing_database_or_wrong_identity_does_not_create_empty_archive(archive, tmp_path):
    engine, _sessions, _media, _events, _original = archive
    engine.dispose()
    database = tmp_path / "archive.db"
    database.rename(tmp_path / "saved.db")
    with pytest.raises(RuntimeError, match="missing"):
        create_database(f"sqlite:///{database}")
    assert not database.exists()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        create_database(f"sqlite:///{database}")


def test_snapshot_pending_events_and_verified_restore(archive, tmp_path):
    engine, _sessions, media, events, _original = archive
    (events / "pending.json").write_text('{"type": "chat", "jid": "pending@g.us"}', "utf-8")
    snapshot = create_snapshot(engine, media, events, tmp_path / "backups")
    manifest = verify_snapshot(snapshot)
    assert "pending.json" in manifest["pending_events"]
    restore_snapshot(snapshot, tmp_path / "restored")
    assert verify_snapshot(tmp_path / "restored") == manifest
    assert (tmp_path / "restored" / "whatsapp-events" / "pending.json").read_bytes() == (events / "pending.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        restore_snapshot(snapshot, tmp_path / "restored")
    (snapshot / "media" / "attachment.bin").write_bytes(b"Corrupted")
    with pytest.raises(ValueError, match="checksum"):
        restore_snapshot(snapshot, tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("failure", ["missing-media", "corrupt-media", "disk-full", "interrupted-copy"])
def test_failed_snapshot_is_never_published_and_previous_snapshot_survives(archive, tmp_path, failure):
    engine, _sessions, media, events, _original = archive
    backup_root = tmp_path / "backups"
    first = create_snapshot(engine, media, events, backup_root)
    if failure == "missing-media":
        (media / "attachment.bin").unlink()
    elif failure == "corrupt-media":
        (media / "attachment.bin").write_bytes(b"Corrupted")
    with patch("whatsapp_archive.protection.shutil.copyfile", wraps=shutil.copyfile) as copy:
        if failure == "disk-full":
            copy.side_effect = OSError(28, "No space left on device")
        elif failure == "interrupted-copy":
            copy.side_effect = InterruptedError("Interrupted")
        with pytest.raises((ValueError, OSError)):
            create_snapshot(engine, media, events, backup_root)
    assert list(backup_root.iterdir()) == [first]
    verify_snapshot(first)


def load_app(monkeypatch, tmp_path):
    data = tmp_path / "app-data"
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{data / 'archive.db'}")
    monkeypatch.setenv("MEDIA_DIR", str(data / "media"))
    monkeypatch.setenv("WHATSAPP_EVENTS_DIR", str(data / "whatsapp-events"))
    monkeypatch.setenv("ARCHIVE_BACKUP_DIR", str(data / "backups"))
    import whatsapp_archive.app as app_module
    return importlib.reload(app_module)


def same_origin_request():
    return SimpleNamespace(headers={"origin": "http://127.0.0.1:8000"}, base_url="http://127.0.0.1:8000/")


def test_pairing_reset_requires_successful_snapshot_and_retains_archive(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    original = {"type": "message", "jid": "1@g.us", "sourceId": "1", "text": "Keep",
                "timestamp": "2025-01-01T00:00:00Z"}
    ingest_whatsapp_event(original, app.SessionFactory, app.MEDIA_DIR)
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "logged_out"})
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return b'{"state":"pairing_required"}'

    def bridge(request, timeout):
        snapshot_name = request.get_header("X-archive-snapshot")
        assert snapshot_name
        verify_snapshot(app.BACKUP_DIR / snapshot_name)
        calls.append(request)
        return Response()

    monkeypatch.setattr(app, "urlopen", bridge)
    response = app.reset_whatsapp_pairing(same_origin_request())
    assert response.status_code == 200 and len(calls) == 1
    with app.SessionFactory() as session:
        assert session.scalar(select(Message)).text == "Keep"
    monkeypatch.setattr(app, "create_snapshot", lambda *args: (_ for _ in ()).throw(OSError("Disk full")))
    with pytest.raises(HTTPException) as error:
        app.reset_whatsapp_pairing(same_origin_request())
    assert error.value.status_code == 503
    assert len(calls) == 1
    app.engine.dispose()


def test_all_messages_are_reachable_offline_with_keyset_pagination(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "logged_out"})
    for number in range(205):
        ingest_whatsapp_event(
            {"type": "message", "jid": "1@g.us", "sourceId": str(number), "text": f"Saved {number:03d}",
             "timestamp": "2025-01-01T00:00:00Z"}, app.SessionFactory, app.MEDIA_DIR,
        )
    with app.SessionFactory() as session:
        chat_id = session.scalar(select(Chat.id))
    for before, expected in ((None, range(105, 205)), (106, range(5, 105)), (6, range(5))):
        page = app.chat_view(chat_id, "", before).body.decode()
        for number in expected:
            assert f"Saved {number:03d}" in page
        assert ("Ältere Nachrichten" in page) == (before != 6)
    app.engine.dispose()


def test_contact_mapping_survives_session_loss(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    ingest_whatsapp_event(
        {"type": "contact_mapping", "lid": "123@lid", "phoneJid": "456@s.whatsapp.net"},
        app.SessionFactory, app.MEDIA_DIR,
    )
    monkeypatch.setattr(app, "urlopen", lambda *args, **kwargs: pytest.fail("Stored mapping must work offline"))
    assert app._resolve_lid_phone_jids([(1, "123@lid", "identity", 1, None)]) == {"123@lid": "456@s.whatsapp.net"}
    app.engine.dispose()


def test_previous_content_remains_visible_and_searchable_after_unreadable_live_update(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "logged_out"})
    original = {"type": "message", "jid": "1@g.us", "sourceId": "1", "text": "Must remain searchable",
                "timestamp": "2025-01-01T00:00:00Z"}
    ingest_whatsapp_event(original, app.SessionFactory, app.MEDIA_DIR)
    ingest_whatsapp_event(
        {**original, "text": "", "rawData": {"message": {"secretEncryptedMessage": {}}}},
        app.SessionFactory, app.MEDIA_DIR,
    )
    with app.SessionFactory() as session:
        chat_id = session.scalar(select(Chat.id))
    page = app.chat_view(chat_id, "searchable").body.decode()
    assert "Must remain searchable" in page
    assert f'href="/chats/{chat_id}"' in page
    app.engine.dispose()


def test_backup_route_requires_same_origin_and_rejects_pairing_when_connected(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as error:
        app.secure_archive(SimpleNamespace(headers={}, base_url="http://127.0.0.1:8000/"))
    assert error.value.status_code == 403
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "connected"})
    with pytest.raises(HTTPException) as error:
        app.reset_whatsapp_pairing(same_origin_request())
    assert error.value.status_code == 409
    assert not app.BACKUP_DIR.exists()
    app.engine.dispose()


def test_history_sender_uses_top_level_participant_and_existing_contact_without_rewriting(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "connected"})
    monkeypatch.setattr(app, "_resolve_lid_phone_jids", lambda *args: {})
    group_jid = "120363427522330874@g.us"
    lid = "127144655077428@lid"
    ingest_whatsapp_event(
        {"type": "chat", "jid": lid, "title": "Archived person"},
        app.SessionFactory, app.MEDIA_DIR,
    )
    ingest_whatsapp_event(
        {"type": "message", "jid": group_jid, "title": "Archived group", "sourceId": "history:in:",
         "sender": group_jid, "text": "Bitte 😘", "timestamp": "2026-10-09T20:25:20Z",
         "rawData": {"key": {"remoteJid": group_jid, "fromMe": False, "id": "history"}, "participant": lid}},
        app.SessionFactory, app.MEDIA_DIR,
    )
    with app.SessionFactory() as session:
        stored = session.scalar(select(Message))
        group_id = stored.chat_id
        assert app._archived_sender(stored) == lid
    page = app.chat_view(group_id, "").body.decode()
    assert "<strong>Archived person</strong>" in page
    assert f"<strong>{group_jid}</strong>" not in page
    with app.SessionFactory() as session:
        stored = session.scalar(select(Message))
        assert stored.sender == group_jid
        assert stored.source_key == "history:in:"
        assert session.scalar(select(MessageVersion)) is None
    app.engine.dispose()


def test_local_alias_survives_whatsapp_chat_message_and_restart(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "connected"})
    monkeypatch.setattr(app, "_resolve_lid_phone_jids", lambda *args: {})
    original = {"type": "message", "jid": "123@lid", "title": "WhatsApp name",
                "sourceId": "original", "text": "Saved message", "timestamp": "2025-01-01T00:00:00Z"}
    ingest_whatsapp_event(original, app.SessionFactory, app.MEDIA_DIR)
    with app.SessionFactory() as session:
        chat_id = session.scalar(select(Chat.id))
    app.rename_chat(chat_id, same_origin_request(), "My local alias")
    ingest_whatsapp_event({"type": "chat", "jid": "123@lid", "title": "Changed WhatsApp name"},
                          app.SessionFactory, app.MEDIA_DIR)
    ingest_whatsapp_event({**original, "title": "Another WhatsApp name"}, app.SessionFactory, app.MEDIA_DIR)
    restarted, sessions = create_database(str(app.engine.url))
    try:
        with sessions() as session:
            saved = session.get(Chat, chat_id)
            assert saved.alias == "My local alias"
            assert saved.title == "Another WhatsApp name"
        page = app.chat_view(chat_id, "").body.decode()
        assert "<h2>My local alias</h2>" in page
        assert "<span>My local alias</span>" in page
        assert 'value="My local alias"' in page
        assert "!aliasEditInProgress()" in page
        assert "input.value !== input.defaultValue" in page
        script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = """
const assert = require('node:assert/strict');
let reloads = 0;
let submitted;
const form = {contains: element => element === input, addEventListener: (_event, handler) => {submitted = handler;}};
const input = {value: 'Typing alias', defaultValue: 'My local alias', closest: () => form};
const element = {textContent: '', hidden: false, replaceChildren() {}};
global.document = {
  activeElement: null, addEventListener() {},
  querySelector: selector => selector.endsWith('input') ? input : form,
  querySelectorAll: () => [], getElementById: () => element,
};
global.navigator = {language: 'de'};
global.window = {location: {reload: () => reloads++}, setInterval() {}};
global.fetch = async () => ({ok: true, json: async () => ({
  state: 'connected', live_revision: 999, archived_chats: 1, archived_messages: 1,
})});
"""
        checks = """
(async () => {
  await refreshWhatsAppStatus(); await refreshWhatsAppStatus();
  assert.equal(reloads, 0, 'dirty alias must survive background refresh');
  input.value = input.defaultValue; document.activeElement = input;
  await refreshWhatsAppStatus();
  assert.equal(reloads, 0, 'focused alias must survive background refresh');
  document.activeElement = null;
  await refreshWhatsAppStatus();
  assert.equal(reloads, 1, 'normal refresh must resume');
  submitted();
  await refreshWhatsAppStatus();
  assert.equal(reloads, 1, 'submission must not be interrupted');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        subprocess.run(["node", "-e", harness + script + checks], check=True, capture_output=True, text=True)
        app.rename_chat(chat_id, same_origin_request(), "Updated alias")
        assert "<h2>Updated alias</h2>" in app.chat_view(chat_id, "").body.decode()
    finally:
        restarted.dispose()
        app.engine.dispose()


def test_local_alias_overrides_phone_mapping_name(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "connected"})
    monkeypatch.setattr(app, "_resolve_lid_phone_jids", lambda *args: {"123@lid": "456@s.whatsapp.net"})
    for jid, title in (("123@lid", "123@lid"), ("456@s.whatsapp.net", "WhatsApp contact")):
        ingest_whatsapp_event({"type": "chat", "jid": jid, "title": title}, app.SessionFactory, app.MEDIA_DIR)
    with app.SessionFactory() as session:
        lid_id = session.scalar(select(Chat.id).where(Chat.title == "123@lid"))
        phone_id = session.scalar(select(Chat.id).where(Chat.title == "WhatsApp contact"))
    app.rename_chat(phone_id, same_origin_request(), "Phone alias")
    assert "<h2>Phone alias</h2>" in app.chat_view(lid_id, "").body.decode()
    app.rename_chat(lid_id, same_origin_request(), "LID alias")
    assert "<h2>LID alias</h2>" in app.chat_view(lid_id, "").body.decode()
    app.engine.dispose()


@pytest.mark.parametrize("kind", ["imageMessage", "videoMessage", "audioMessage", "documentMessage",
                                 "stickerMessage", "ptvMessage", "extendedTextMessage"])
@pytest.mark.parametrize("wrapped", [None, "ephemeralMessage", "associatedChildMessage", "lottieStickerMessage"])
def test_missing_media_has_retry_and_endpoint_accepts_all_types(monkeypatch, tmp_path, kind, wrapped):
    app = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "connected"})
    monkeypatch.setattr(app, "_resolve_lid_phone_jids", lambda *args: {})
    content = {kind: {"url": "https://example.invalid/media"}}
    if kind == "extendedTextMessage":
        content = {kind: {"thumbnailDirectPath": "/preview"}}
    if wrapped:
        content = {wrapped: {"message": content}}
    raw = {"key": {"remoteJid": "1@g.us", "id": "media"}, "message": content}
    ingest_whatsapp_event(
        {"type": "message", "jid": "1@g.us", "sourceId": "media:in:", "text": "Media",
         "timestamp": "2025-01-01T00:00:00Z", "rawData": raw}, app.SessionFactory, app.MEDIA_DIR,
    )
    with app.SessionFactory() as session:
        message = session.scalar(select(Message))
        mid, cid = message.id, message.chat_id
    assert f'data-message-id="{mid}"' in app.chat_view(cid, "").body.decode()
    calls = []

    def unavailable(request, timeout):
        from urllib.error import HTTPError
        from io import BytesIO
        calls.append(json.loads(request.data))
        raise HTTPError(request.full_url, 503, "Unavailable", {}, BytesIO(
            b'{"error":"WhatsApp file is unavailable"}'
        ))

    monkeypatch.setattr(app, "urlopen", unavailable)
    response = app.retry_video_media(mid, same_origin_request())
    assert response.status_code == 503
    assert json.loads(response.body)["error"] == "WhatsApp file is unavailable"
    assert calls == [{"message": raw}]
    with app.SessionFactory() as session:
        assert session.get(Message, mid).source_key == "media:in:"
    app.engine.dispose()


def test_successful_media_retry_adds_file_without_replacing_existing_text(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    original = {
        "type": "message", "jid": "1@g.us", "sourceId": "photo:in:",
        "timestamp": "2025-01-01T00:00:00Z", "text": "Keep original caption",
        "rawData": {"key": {"remoteJid": "1@g.us", "id": "photo"}, "message": {"imageMessage": {}}},
    }
    ingest_whatsapp_event(original, app.SessionFactory, app.MEDIA_DIR)
    with app.SessionFactory() as session:
        mid = session.scalar(select(Message.id))

    class Response:
        def __enter__(self):
            payload = b"Recovered media"
            (app.MEDIA_DIR / "recovered.bin").write_bytes(payload)
            event = {**original, "historical": True, "text": "Different retry caption",
                     "media": {"path": "recovered.bin", "sha256": hashlib.sha256(payload).hexdigest()}}
            (app.WHATSAPP_EVENTS_DIR / "retry.json").write_text(json.dumps(event), "utf-8")
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return b'{"state":"media_retried"}'

    monkeypatch.setattr(app, "urlopen", lambda *args, **kwargs: Response())
    assert app.retry_video_media(mid, same_origin_request()).status_code == 200
    with app.SessionFactory() as session:
        assert session.get(Message, mid).text == "Keep original caption"
        assert session.get(Message, mid).source_key == "photo:in:"
        assert session.scalar(select(Media)).message_id == mid
        assert len(session.scalars(select(Message)).all()) == 1
    app.engine.dispose()


def test_quoted_media_retry_selects_reference_and_verifies_its_hash(monkeypatch, tmp_path):
    app = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_whatsapp_status", lambda: {"state": "connected"})
    monkeypatch.setattr(app, "_resolve_lid_phone_jids", lambda *args: {})
    payload = b"Quoted picture"
    digest = hashlib.sha256(payload).hexdigest()
    raw = {
        "key": {"remoteJid": "1@g.us", "id": "reply"},
        "message": {"imageMessage": {
            "caption": "Keep reply", "url": "https://example.invalid/main",
            "mediaKey": base64.b64encode(b"main-key").decode(),
            "fileSha256": base64.b64encode(b"x" * 32).decode(), "contextInfo": {
            "stanzaId": "quoted", "participant": "2@s.whatsapp.net",
            "quotedMessage": {"imageMessage": {
                "mediaKey": base64.b64encode(b"key").decode(),
                "url": "https://example.invalid",
                "fileSha256": base64.b64encode(bytes.fromhex(digest)).decode(),
            }},
        }}},
    }
    event = {"type": "message", "jid": "1@g.us", "sourceId": "reply:in:",
             "timestamp": "2025-01-01T00:00:00Z", "text": "Keep reply", "rawData": raw}
    ingest_whatsapp_event(event, app.SessionFactory, app.MEDIA_DIR)
    with app.SessionFactory() as session:
        mid = session.scalar(select(Message.id))
    part = "message.imageMessage.contextInfo.quotedMessage.imageMessage"
    calls = []

    class Response:
        def __enter__(self):
            (app.MEDIA_DIR / "quoted.jpg").write_bytes(payload)
            recovered = {**event, "historical": True,
                         "media": {"path": "quoted.jpg", "sha256": digest}}
            (app.WHATSAPP_EVENTS_DIR / "quoted.json").write_text(json.dumps(recovered), "utf-8")
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return b'{"state":"media_retried"}'

    def request_media(request, timeout):
        calls.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(app, "urlopen", request_media)
    assert app.retry_video_media(mid, same_origin_request(), part).status_code == 200
    assert calls == [{"message": raw, "part": part}]
    assert app.retry_video_media(mid, same_origin_request(), part).status_code == 200
    assert len(calls) == 1
    with app.SessionFactory() as session:
        assert session.get(Message, mid).raw_data == json.dumps(raw, ensure_ascii=False)
        assert session.get(Message, mid).text == "Keep reply"
        assert len(session.scalars(select(Message)).all()) == 1
        chat_id = session.get(Message, mid).chat_id
    assert f'data-message-id="{mid}">Bild erneut laden' in app.chat_view(chat_id, "").body.decode()
    assert app.retry_video_media(mid, same_origin_request()).status_code == 503
    assert calls[-1] == {"message": raw}
    with pytest.raises(HTTPException) as error:
        app.retry_video_media(mid, same_origin_request(), "message.unknown.imageMessage")
    assert error.value.status_code == 400
    app.engine.dispose()
