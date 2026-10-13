import hashlib
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_root_page_renders_and_keeps_js_template_literal(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("MEDIA_DIR", str(data_dir / "media"))
    monkeypatch.setenv("WHATSAPP_EVENTS_DIR", str(data_dir / "whatsapp-events"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{data_dir / 'archive.db'}")

    import whatsapp_archive.app as app_module
    from whatsapp_archive.models import Base, Chat, Media, Message

    importlib.reload(app_module)
    Base.metadata.create_all(bind=app_module.engine)
    monkeypatch.setattr(
        app_module,
        "_whatsapp_status",
        lambda: {
            "state": "logged_out",
            "paired": False,
            "qrDataUrl": None,
            "lastError": "Session logged out",
            "archived_chats": 0,
            "archived_messages": 0,
            "history": {},
        },
    )

    monkeypatch.setattr(
        app_module,
        "_resolve_lid_phone_jids",
        lambda _chats, _senders=(): {"123456789@lid": "123456789@s.whatsapp.net"},
    )

    with app_module.SessionFactory() as session:
        chat = Chat(identity_key="chat-1", title="Smoke Test")
        session.add(chat)
        session.flush()
        chat_id = chat.id
        system_chat = Chat(identity_key="system-only-chat", title="Systemereignis")
        session.add(system_chat)
        session.flush()
        session.add(
            Message(
                chat_id=system_chat.id,
                identity_key="system-only-message",
                sent_at=datetime(2025, 12, 31, 23, 59),
                sender="System",
                text="",
                raw_data=json.dumps({"messageStubType": "GROUP_PARTICIPANT_ADD"}),
            )
        )
        session.add(
            Message(
                chat_id=system_chat.id,
                identity_key="system-chat-real-message",
                sent_at=datetime(2025, 12, 31, 22, 15),
                sender="123456789@lid",
                text="Earlier real message",
            )
        )
        lid_chat = Chat(identity_key="chat-lid", title="123456789@lid")
        session.add(lid_chat)
        session.add(
            Chat(
                identity_key=hashlib.sha256(b"123456789@s.whatsapp.net").hexdigest(),
                title="Emilian Weis",
            )
        )
        session.flush()
        lid_chat_id = lid_chat.id
        session.add(
            Message(
                chat_id=lid_chat.id,
                identity_key="lid-message",
                sent_at=datetime(2025, 12, 31, 22, 14),
                sender="123456789@lid",
                text="Namensauflösungstest",
            )
        )
        session.add(
            Message(
                chat_id=lid_chat.id,
                identity_key="lid-deleted-message",
                sent_at=datetime(2025, 12, 31, 23, 59),
                deleted_at=datetime(2025, 12, 31, 23, 59, 1),
                sender="123456789@lid",
                text="Deleted message must not affect chat order",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-1",
                sent_at=datetime(2025, 12, 31, 22, 15),
                sender="Ada",
                text="Live-Ereignis erfolgreich",
                source_key="source-1",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-conversation",
                sent_at=datetime(2025, 12, 31, 22, 16),
                sender="Ada",
                text="[conversation]",
                raw_data=json.dumps({"message": {"conversation": "Readable WhatsApp test"}}),
                source_key="source-2",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-protocol",
                sent_at=datetime(2025, 12, 31, 22, 17),
                sender="Ada",
                text="[protocolMessage]",
                raw_data=json.dumps({"message": {"protocolMessage": {"type": 6}}}),
                source_key="source-3",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-document",
                sent_at=datetime(2025, 12, 31, 22, 18),
                sender="Ada",
                text="[documentMessage]",
                source_key="source-4",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-stub",
                sent_at=datetime(2025, 12, 31, 22, 19),
                sender="Ada",
                text="",
                raw_data=json.dumps({"messageStubType": "REVOKE"}),
                source_key="source-5",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-secret-encrypted",
                sent_at=datetime(2025, 12, 31, 22, 19, 30),
                sender="Ada",
                text="[secretEncryptedMessage]",
                raw_data=json.dumps({"message": {"secretEncryptedMessage": {"encPayload": "ciphertext", "encIv": "iv"}}}),
                source_key="source-secret",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-reaction-target",
                sent_at=datetime(2025, 12, 31, 22, 19, 40),
                sender="Ada",
                text="Original message with reaction",
                source_key="target-message",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-reaction-event",
                sent_at=datetime(2025, 12, 31, 22, 19, 50),
                sender="Patricia",
                text="😂",
                raw_data=json.dumps({"message": {"reactionMessage": {"text": "😂", "key": {"id": "target-message", "remoteJid": "123456789@lid"}}}}),
                source_key="reaction-event",
            )
        )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="msg-text-presentation-reaction",
                sent_at=datetime(2025, 12, 31, 22, 19, 55),
                sender="Patricia",
                text="☺",
                raw_data=json.dumps({"message": {"reactionMessage": {"text": "☺", "key": {"id": "target-message", "remoteJid": "123456789@lid"}}}}),
                source_key="reaction-event-smiley",
            )
        )
        for index, (message_type, mime_type) in enumerate(
            (("imageMessage", "image/jpeg"), ("audioMessage", "audio/ogg; codecs=opus"), ("videoMessage", "video/mp4"), ("documentMessage", "application/pdf")),
            start=1,
        ):
            media_message = Message(
                chat_id=chat.id,
                identity_key=f"media-{message_type}",
                sent_at=datetime(2025, 12, 31, 22, 20 + index),
                sender="Ada",
                text=f"{message_type} test",
                raw_data=json.dumps({"message": {message_type: {"mimetype": mime_type}}}),
                source_key=f"media-source-{message_type}",
            )
            session.add(media_message)
            session.flush()
            session.add(
                Media(
                    message_id=media_message.id,
                    original_name="file.pdf" if message_type == "documentMessage" else f"file.{message_type}",
                    sha256=f"{index:064x}",
                    stored_path=f"whatsapp/test-{message_type}",
                )
            )
        session.add(
            Message(
                chat_id=chat.id,
                identity_key="video-without-media",
                sent_at=datetime(2025, 12, 31, 22, 30),
                sender="Ada",
                text="[videoMessage]",
                raw_data=json.dumps({"message": {"videoMessage": {"mimetype": "video/mp4"}}}),
            )
        )
        session.commit()

    html = app_module._page("", None)

    assert "data-chat-id=\"" in html
    assert "data-message-id=\"" in html
    assert 'href="/chats/1"' in html
    assert "123456789@lid" not in html
    assert "Emilian Weis" in html
    assert "<strong>Emilian Weis</strong>" in html
    assert "Chats · 3" in html
    assert html.index('href="/chats/1"') < html.index('href="/chats/2"')
    assert html.index('href="/chats/2"') < html.index('href="/chats/3"')
    assert app_module._display_chat_name(3, "123456789@lid") == "Unbenannter Kontakt · 003"
    assert app_module._display_chat_name(3, "123456789@s.whatsapp.net") == "+123456789"
    names, _hidden = app_module._chat_view_names(
        [(4, "987654321@lid", "lid-without-contact", 1, None)],
        {"987654321@lid": "987654321@s.whatsapp.net"},
    )
    assert names[4] == "+987654321"
    assert "`/chats/${chatId}`" in html
    assert "Readable WhatsApp test" in html
    assert "Dokument" in html
    assert "Nachricht gelöscht" in html
    assert "[conversation]" not in html
    assert "[protocolMessage]" not in html
    assert "[documentMessage]" not in html
    assert "stableLiveRevisionPolls >= 2" in html
    assert "Archivierte Chats" not in html
    assert "Export importieren" not in html
    assert 'action="/imports"' not in html
    assert "window.location.reload()" in html
    assert '<img src="/media/' in html
    assert '<audio controls preload="none"' in html
    assert '<video controls preload="metadata"' in html
    assert 'class="media-retry"' in html
    assert 'fetch(`/api/media/${button.dataset.messageId}/retry`' in html
    assert 'target="_blank" rel="noopener">PDF öffnen</a>' in html
    assert "Datei herunterladen: file.pdf" in html
    assert "?download=1" in html
    assert 'data-utc="2025-12-31T22:15:00Z"' in html
    assert "Intl.DateTimeFormat(navigator.language" in html
    assert app_module._media_content_type("media.audioMessage") == "audio/ogg; codecs=opus"
    assert app_module._media_content_type("media.videoMessage") == "video/mp4"
    assert app_module._media_response_options("file.pdf")[1] == "inline"
    assert app_module._media_response_options("media.audioMessage", "audio/ogg; codecs=opus")[1] == "inline"
    assert 'id="pairing-reset"' in html
    assert "window.confirm(" in html
    assert "fetch('/api/whatsapp/pairing/reset'" in html
    assert 'id="contact-refresh"' not in html
    assert "fetch('/api/whatsapp/contacts/refresh'" not in html

    response = app_module.chat_view(chat_id, "")
    conversation = response.body.decode()
    assert response.status_code == 200
    assert 'class="chat-list"' in conversation
    assert 'class="chat-link selected" href="/chats/1"' in conversation
    assert 'data-open-chat="false"' in conversation
    assert "← Alle Chats" not in conversation
    assert 'value="Smoke Test"' in conversation
    assert '<span class="reaction">😂</span>' in conversation
    assert '<span class="reaction">☺️</span>' in conversation
    assert "reaction-event" not in conversation
    assert "Verschlüsselte Nachricht (Inhalt nicht verfügbar)" not in conversation
    assert "[secretEncryptedMessage]" not in conversation
    assert "Original message with reaction" in conversation
    assert conversation.index("documentMessage test") < conversation.index("videoMessage test")
    assert conversation.index("videoMessage test") < conversation.index("audioMessage test")

    root_response = app_module.index("", None)
    assert 'class="chat-link selected" href="/chats/1"' in root_response.body.decode()

    with pytest.raises(HTTPException) as error:
        app_module.reset_whatsapp_pairing(
            SimpleNamespace(headers={"origin": "https://external.example"}, base_url="http://127.0.0.1:8000/")
        )
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as retry_error:
        app_module.retry_video_media(
            1,
            SimpleNamespace(headers={"origin": "https://external.example"}, base_url="http://127.0.0.1:8000/"),
        )
    assert retry_error.value.status_code == 403

    monkeypatch.setattr(app_module, "_resolve_lid_phone_jids", lambda _chats, _senders=(): {})
    unknown_contact = app_module.chat_view(lid_chat_id, "").body.decode()
    assert 'action="/chats/3/name"' in unknown_contact
    assert 'name="name"' in unknown_contact
    renamed = app_module.rename_chat(
        lid_chat_id,
        SimpleNamespace(headers={"origin": "http://127.0.0.1:8000"}, base_url="http://127.0.0.1:8000/"),
        "Emilian Weis",
    )
    assert renamed.status_code == 303
    with app_module.SessionFactory() as session:
        assert session.get(Chat, lid_chat_id).title == "Emilian Weis"


def test_live_event_scan_advances_viewer_revision(monkeypatch, tmp_path):
    data_dir = tmp_path / "live-data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("MEDIA_DIR", str(data_dir / "media"))
    monkeypatch.setenv("WHATSAPP_EVENTS_DIR", str(data_dir / "whatsapp-events"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{data_dir / 'archive.db'}")

    import whatsapp_archive.app as app_module

    importlib.reload(app_module)
    events_dir = Path(app_module.WHATSAPP_EVENTS_DIR)
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "chat.json").write_text(
        json.dumps({"type": "chat", "jid": "live-event@test", "title": "Live event"}),
        encoding="utf-8",
    )

    previous_revision = app_module._LIVE_REVISION
    assert app_module._scan_live_events() == 1
    assert app_module._LIVE_REVISION == previous_revision + 1
