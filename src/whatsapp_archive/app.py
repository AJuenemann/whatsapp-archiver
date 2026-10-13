from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import mimetypes
import os
from datetime import timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi import FastAPI, Form, HTTPException, Query
from fastapi import Request as FastAPIRequest
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, case, func, or_, select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .database import create_database
from .importer import scan_whatsapp_events
from .models import Chat, Media, Message, MessageVersion

logger = logging.getLogger("whatsapp_archive")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", DATA_ROOT / "media"))
WHATSAPP_EVENTS_DIR = Path(os.getenv("WHATSAPP_EVENTS_DIR", DATA_ROOT / "whatsapp-events"))
WHATSAPP_BRIDGE_URL = os.getenv("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge:8765").rstrip("/")
DATA_ROOT.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
WHATSAPP_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_ROOT / 'archive.db'}")
engine, SessionFactory = create_database(DATABASE_URL)
app = FastAPI(title="WhatsApp Archive", docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
_LIVE_REVISION = 0

_INLINE_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "video/mp4",
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "application/pdf",
}
_MESSAGE_PLACEHOLDER_LABELS = {
    "[imageMessage]": "Bild",
    "[videoMessage]": "Video",
    "[audioMessage]": "Audio",
    "[documentMessage]": "Dokument",
    "[stickerMessage]": "Sticker",
    "[albumMessage]": "Medienalbum",
    "[pollCreationMessageV3]": "Umfrage",
    "[ptvMessage]": "Video-Nachricht",
    "[lottieStickerMessage]": "Animierter Sticker",
        "[secretEncryptedMessage]": "Verschlüsselte Nachricht (Inhalt nicht verfügbar)",
}
_MESSAGE_STUB_LABELS = {
    "REVOKE": "Nachricht gelöscht",
    "GROUP_PARTICIPANT_ADD": "Teilnehmer hinzugefügt",
    "GROUP_PARTICIPANT_LEAVE": "Teilnehmer hat die Gruppe verlassen",
    "GROUP_PARTICIPANT_CHANGE_NUMBER": "Telefonnummer geändert",
    "GROUP_PARTICIPANT_REMOVE": "Teilnehmer entfernt",
    "E2E_ENCRYPTED": "Ende-zu-Ende-Verschlüsselung",
    "DISAPPEARING_MODE": "Ablaufende Nachrichten geändert",
    "CHANGE_LIMIT_SHARING": "Freigabeeinstellungen geändert",
    "INDIVIDUAL_CHANGE_NUMBER": "Telefonnummer geändert",
    "GROUP_CHANGE_SUBJECT": "Gruppenname geändert",
    "GROUP_CHANGE_ICON": "Gruppenbild geändert",
    "GROUP_CREATE": "Gruppe erstellt",
    "BIZ_PRIVACY_MODE_INIT_FB": "Unternehmenseinstellungen geändert",
    "GROUP_CHANGE_DESCRIPTION": "Gruppenbeschreibung geändert",
}


def _media_content_type(filename: str, raw_data: str | None = None) -> str:
    guessed = mimetypes.guess_type(filename)[0]
    if guessed:
        return guessed
    fallback_types = {
        ".imagemessage": "image/jpeg",
        ".audiomessage": "audio/ogg; codecs=opus",
        ".videomessage": "video/mp4",
        ".stickermessage": "image/webp",
    }
    fallback = fallback_types.get(Path(filename).suffix.lower())
    if fallback:
        return fallback
    if raw_data:
        try:
            message = json.loads(raw_data).get("message") or {}
        except (TypeError, ValueError):
            message = {}
        if isinstance(message, dict):
            for item in message.values():
                if isinstance(item, dict) and isinstance(item.get("mimetype"), str):
                    return item["mimetype"]
    return "application/octet-stream"


def _media_response_options(filename: str, content_type: str | None = None) -> tuple[str, str, dict[str, str]]:
    media_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    inline = media_type.split(";", 1)[0].strip().lower() in _INLINE_MEDIA_TYPES
    headers = {"X-Content-Type-Options": "nosniff"}
    if inline:
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return media_type, "inline" if inline else "attachment", headers


def _render_media(media: Media, message: Message) -> str:
    url = f"/media/{media.id}"
    media_type = _media_content_type(media.original_name, message.raw_data)
    base_type = media_type.split(";", 1)[0].strip().lower()
    preview = ""
    if base_type.startswith("image/"):
        preview = f'<img src="{url}" loading="lazy" alt="{html.escape(media.original_name, quote=True)}">'
    elif base_type.startswith("audio/"):
        preview = f'<audio controls preload="none" src="{url}">Audio kann nicht abgespielt werden.</audio>'
    elif base_type.startswith("video/"):
        preview = f'<video controls preload="metadata" playsinline src="{url}">Video kann nicht abgespielt werden.</video>'
    elif base_type == "application/pdf":
        preview = f'<a class="attachment" href="{url}" target="_blank" rel="noopener">PDF öffnen</a>'

    labels = {
        ".imagemessage": "Bild",
        ".audiomessage": "Audio",
        ".videomessage": "Video",
        ".stickermessage": "Sticker",
    }
    filename = html.escape(media.original_name)
    label = labels.get(Path(media.original_name).suffix.lower(), filename)
    return f'<div class="media-preview">{preview}<a class="attachment" href="{url}?download=1">Datei herunterladen: {label}</a></div>'


async def _live_event_watcher() -> None:
    while True:
        await run_in_threadpool(_scan_live_events)
        await asyncio.sleep(4)


def _scan_live_events() -> int:
    global _LIVE_REVISION
    processed = scan_whatsapp_events(WHATSAPP_EVENTS_DIR, SessionFactory, MEDIA_DIR)
    _LIVE_REVISION += processed
    return processed


@app.on_event("startup")
async def start_live_event_watcher() -> None:
    app.state.watcher = asyncio.create_task(_live_event_watcher())


@app.on_event("shutdown")
async def stop_live_event_watcher() -> None:
    watcher = getattr(app.state, "watcher", None)
    if watcher is not None:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    engine.dispose()


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _whatsapp_status() -> dict:
    try:
        request = Request(f"{WHATSAPP_BRIDGE_URL}/status", headers={"Accept": "application/json"})
        with urlopen(request, timeout=1) as response:
            status = json.load(response)
    except (OSError, ValueError):
        status = {"state": "bridge_unavailable", "paired": False, "history": {}}

    with SessionFactory() as session:
        status["archived_chats"] = session.scalar(select(func.count(Chat.id))) or 0
        status["archived_messages"] = session.scalar(
            select(func.count(Message.id)).where(Message.source_key.is_not(None))
        ) or 0
    status["live_revision"] = _LIVE_REVISION
    return status


@app.get("/api/whatsapp/status")
def whatsapp_connection_status() -> JSONResponse:
    return JSONResponse(_whatsapp_status(), headers={"Cache-Control": "no-store"})


@app.post("/api/whatsapp/pairing/reset")
def reset_whatsapp_pairing(request: FastAPIRequest) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin request required")
    request = Request(
        f"{WHATSAPP_BRIDGE_URL}/pairing/reset",
        data=b"",
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return JSONResponse(json.load(response), headers={"Cache-Control": "no-store"})
    except HTTPError as error:
        try:
            detail = json.loads(error.read())
        except (ValueError, OSError):
            detail = {"error": "Pairing reset rejected"}
        return JSONResponse(detail, status_code=error.code, headers={"Cache-Control": "no-store"})
    except Exception as error:
        raise HTTPException(status_code=503, detail="Could not start local pairing") from error


@app.post("/api/media/{message_id}/retry")
def retry_video_media(message_id: int, request: FastAPIRequest) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin request required")
    with SessionFactory() as session:
        message = session.get(Message, message_id)
        if message is None or not message.raw_data:
            raise HTTPException(status_code=404, detail="Video not found")
        try:
            raw_data = json.loads(message.raw_data)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="Video data is invalid") from error
        content = raw_data.get("message") if isinstance(raw_data, dict) else None
        if not isinstance(content, dict) or "videoMessage" not in content:
            raise HTTPException(status_code=400, detail="Message is not a video")
        if session.scalar(select(Media.id).where(Media.message_id == message_id)) is not None:
            return JSONResponse({"state": "already_available"})

    bridge_request = Request(
        f"{WHATSAPP_BRIDGE_URL}/media/retry",
        data=json.dumps({"message": raw_data}).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(bridge_request, timeout=180) as response:
            result = json.load(response)
        _scan_live_events()
        with SessionFactory() as session:
            available = session.scalar(select(Media.id).where(Media.message_id == message_id)) is not None
        if not available:
            return JSONResponse({"error": "Video is not available"}, status_code=503)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except HTTPError as error:
        return JSONResponse({"error": "Video could not be downloaded"}, status_code=error.code)
    except Exception as error:
        raise HTTPException(status_code=503, detail="Could not retry video download") from error


def _display_message_text(message: Message) -> str:
    if message.text in _MESSAGE_PLACEHOLDER_LABELS:
        return _MESSAGE_PLACEHOLDER_LABELS[message.text]
    if message.text not in {"[conversation]", ""} or not message.raw_data:
        return message.text
    try:
        raw_data = json.loads(message.raw_data)
    except (TypeError, ValueError):
        return message.text
    content = raw_data.get("message") if isinstance(raw_data, dict) else None
    text = content.get("conversation") if isinstance(content, dict) else None
    if isinstance(text, str):
        return text
    if not message.text:
        stub_type = raw_data.get("messageStubType")
        if stub_type:
            return _MESSAGE_STUB_LABELS.get(stub_type, "WhatsApp-Systemereignis")
    return message.text


def _reaction_details(message: Message) -> tuple[str, str] | None:
    if not message.raw_data or "reactionMessage" not in message.raw_data:
        return None
    try:
        raw_data = json.loads(message.raw_data)
    except (TypeError, ValueError):
        return None
    content = raw_data.get("message") if isinstance(raw_data, dict) else None
    reaction = content.get("reactionMessage") if isinstance(content, dict) else None
    key = reaction.get("key") if isinstance(reaction, dict) else None
    target_id = key.get("id") if isinstance(key, dict) else None
    text = reaction.get("text") if isinstance(reaction, dict) else None
    if isinstance(target_id, str) and target_id and isinstance(text, str) and text:
        return target_id, text
    return None


def _chat_activity_predicate():
    return and_(
        Message.deleted_at.is_(None),
        Message.text != "",
        Message.text != "[protocolMessage]",
        or_(Message.raw_data.is_(None), ~Message.raw_data.contains('"reactionMessage"')),
        or_(Message.raw_data.is_(None), ~Message.raw_data.contains('"secretEncryptedMessage"')),
        or_(Message.raw_data.is_(None), ~Message.raw_data.contains('"messageStubType"')),
    )


def _display_reaction(text: str) -> str:
    result = []
    for index, character in enumerate(text):
        result.append(character)
        if character == "\u263a" and (index + 1 == len(text) or text[index + 1] not in {"\ufe0e", "\ufe0f"}):
            result.append("\ufe0f")
    return "".join(result)


def _display_chat_name(chat_id: int, title: str) -> str:
    identifier, separator, domain = title.rpartition("@")
    if separator and identifier.isdigit() and domain in {"s.whatsapp.net", "c.us"}:
        return f"+{identifier}"
    if separator and identifier.isdigit() and domain == "lid":
        return f"Unbenannter Kontakt · {chat_id:03d}"
    return title


def _resolve_lid_phone_jids(chats: list[tuple], senders: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    jids = [title for _chat_id, title, *_rest in chats if title.endswith("@lid") and title[:-4].isdigit()]
    jids.extend(sender for sender in senders if sender.endswith("@lid") and sender[:-4].isdigit())
    jids = list(dict.fromkeys(jids))
    if not jids:
        return {}
    resolved: dict[str, str] = {}
    for start in range(0, len(jids), 200):
        batch = jids[start : start + 200]
        request = Request(
            f"{WHATSAPP_BRIDGE_URL}/resolve-lids",
            data=json.dumps({"jids": batch}).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=1) as response:
                resolved.update(json.load(response))
        except (OSError, ValueError):
            continue
    return {
        lid: phone
        for lid, phone in resolved.items()
        if lid in jids and isinstance(phone, str) and phone.endswith("@s.whatsapp.net")
    }


def _chat_view_names(chats: list[tuple], lid_phone_jids: dict[str, str]) -> tuple[dict[int, str], set[int]]:
    chats_by_identity = {
        identity_key: (chat_id, title, message_count)
        for chat_id, title, identity_key, message_count, _last_message_at in chats
    }
    display_names: dict[int, str] = {}
    hidden_chat_ids: set[int] = set()
    for chat_id, title, identity_key, message_count, _last_message_at in chats:
        phone_jid = lid_phone_jids.get(title)
        if not phone_jid:
            continue
        identity_key = hashlib.sha256(phone_jid.strip().casefold().encode("utf-8")).hexdigest()
        contact = chats_by_identity.get(identity_key)
        if contact is None:
            display_names[chat_id] = _display_chat_name(chat_id, phone_jid)
            continue
        contact_id, contact_name, contact_message_count = contact
        contact_display_name = _display_chat_name(contact_id, contact_name)
        display_names[chat_id] = contact_display_name
        if chat_id != contact_id and message_count > 0 and contact_message_count == 0:
            hidden_chat_ids.add(contact_id)
        elif chat_id != contact_id and message_count == 0 and contact_message_count > 0:
            hidden_chat_ids.add(chat_id)
    return display_names, hidden_chat_ids


def _page(query: str, selected_chat: int | None) -> str:
    whatsapp_status = _whatsapp_status()
    state_labels = {
        "bridge_unavailable": "Bridge nicht erreichbar",
        "starting": "Verbindung startet",
        "pairing_required": "QR-Kopplung erforderlich",
        "connected": "Verbunden",
        "syncing_history": "Chatverlauf wird synchronisiert",
        "reconnecting": "Verbindung wird wiederhergestellt",
        "logged_out": "Abgemeldet",
        "error": "Verbindungsfehler",
        "mock": "Testmodus",
    }
    state_label = html.escape(state_labels.get(whatsapp_status.get("state"), "Unbekannter Status"))
    qr_data_url = whatsapp_status.get("qrDataUrl")
    qr_panel = ""
    if qr_data_url:
        qr_panel = (
            f'<div class="pairing"><img src="{html.escape(qr_data_url, quote=True)}" alt="WhatsApp-Verknüpfungs-QR-Code">'
            '<span>In WhatsApp unter Verknüpfte Geräte scannen. Den Code nicht weitergeben.</span></div>'
        )
    elif whatsapp_status.get("state") == "logged_out":
        qr_panel = (
            '<div class="pairing"><button type="button" id="pairing-reset">Neue lokale Kopplung</button>'
            '<span>Setzt nur die lokale Sitzung zurück und fordert einen neuen QR-Code an.</span></div>'
        )
    archive_counts = "{} Chats · {} Nachrichten im Archiv".format(
        whatsapp_status.get("archived_chats", 0), whatsapp_status.get("archived_messages", 0)
    )
    history_progress = (whatsapp_status.get("history") or {}).get("progress")
    if history_progress is not None:
        archive_counts += f" · Synchronisierung {html.escape(str(history_progress))}%"
    bridge_error = whatsapp_status.get("lastError")
    bridge_error_html = (
        f'<small id="connection-error" class="connection-error">{html.escape(str(bridge_error))}</small>'
        if bridge_error
        else '<small id="connection-error" class="connection-error" hidden></small>'
    )
    with SessionFactory() as session:
        chats = session.execute(
            select(
                Chat.id,
                Chat.title,
                Chat.identity_key,
                func.count(Message.id),
                func.max(case((_chat_activity_predicate(), Message.sent_at), else_=None)),
            )
            .outerjoin(Message, Message.chat_id == Chat.id)
            .where(or_(Message.id.is_(None), Message.text != "[protocolMessage]"))
            .where(or_(Message.raw_data.is_(None), ~Message.raw_data.contains('"reactionMessage"')))
            .where(or_(Message.raw_data.is_(None), ~Message.raw_data.contains('"secretEncryptedMessage"')))
            .group_by(Chat.id, Chat.title, Chat.identity_key)
            .order_by(Chat.title.collate("NOCASE"))
        ).all()
        reactions_by_message: dict[int, list[str]] = {}
        if selected_chat is not None:
            source_ids = {
                source_key.split(":", 1)[0]: message_id
                for message_id, source_key in session.execute(
                    select(Message.id, Message.source_key).where(
                        Message.chat_id == selected_chat,
                        Message.source_key.is_not(None),
                    )
                )
            }
            reaction_rows = session.execute(
                select(Message).where(
                    Message.chat_id == selected_chat,
                    Message.raw_data.contains('"reactionMessage"'),
                )
            ).scalars()
            for reaction_message in reaction_rows:
                details = _reaction_details(reaction_message)
                target_message_id = source_ids.get(details[0]) if details else None
                if target_message_id is not None:
                    reactions_by_message.setdefault(target_message_id, []).append(details[1])

        statement = (
            select(Message, Chat.title)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Message.text != "[protocolMessage]")
            .where(or_(Message.raw_data.is_(None), ~Message.raw_data.contains('"reactionMessage"')))
            .where(or_(Message.raw_data.is_(None), ~Message.raw_data.contains('"secretEncryptedMessage"')))
        )
        if selected_chat is not None:
            statement = statement.where(Message.chat_id == selected_chat)
        if query:
            pattern = f"%{query}%"
            statement = statement.where(or_(Message.text.ilike(pattern), Message.sender.ilike(pattern)))
        messages = session.execute(statement.order_by(Message.sent_at.desc(), Message.id.desc()).limit(100)).all()
        message_ids = [message.id for message, _chat_title in messages]
        media_rows = (
            session.scalars(select(Media).where(Media.message_id.in_(message_ids))).all() if message_ids else []
        )
        version_rows = (
            session.scalars(select(MessageVersion).where(MessageVersion.message_id.in_(message_ids))).all()
            if message_ids
            else []
        )
        media_by_message: dict[int, list[Media]] = {}
        for media in media_rows:
            media_by_message.setdefault(media.message_id, []).append(media)
        versions_by_message: dict[int, list[MessageVersion]] = {}
        for version in version_rows:
            versions_by_message.setdefault(version.message_id, []).append(version)

    lid_phone_jids = _resolve_lid_phone_jids(chats, tuple(message.sender for message, _title in messages if message.sender))
    resolved_chat_names, hidden_chat_ids = _chat_view_names(chats, lid_phone_jids)
    visible_chats = [chat for chat in chats if chat[0] not in hidden_chat_ids]
    display_name_by_chat_id = {
        chat_id: resolved_chat_names.get(chat_id, _display_chat_name(chat_id, title))
        for chat_id, title, _identity_key, _message_count, _last_message_at in visible_chats
    }
    visible_chats.sort(
        key=lambda row: (row[4] is not None, row[4], display_name_by_chat_id[row[0]].casefold(), row[0]),
        reverse=True,
    )
    chat_display_name_by_id = {
        chat_id: resolved_chat_names.get(chat_id, _display_chat_name(chat_id, title))
        for chat_id, title, _identity_key, _message_count, _last_message_at in chats
    }
    display_name_by_jid = {
        title: chat_display_name_by_id[chat_id]
        for chat_id, title, _identity_key, _message_count, _last_message_at in chats
    }
    contact_by_identity = {identity_key: (chat_id, title) for chat_id, title, identity_key, *_rest in chats}
    for lid, phone in lid_phone_jids.items():
        phone_identity = hashlib.sha256(phone.strip().casefold().encode("utf-8")).hexdigest()
        contact = contact_by_identity.get(phone_identity)
        if contact is None:
            display_name_by_jid[lid] = _display_chat_name(0, phone)
            continue
        contact_id, contact_title = contact
        display_name_by_jid[lid] = resolved_chat_names.get(
            contact_id,
            _display_chat_name(contact_id, contact_title),
        )

    chat_links = [
        '<a class="chat-link{}" href="/chats/{}"><span>{}</span><small>{}</small></a>'.format(
            " selected" if chat_id == selected_chat else "",
            chat_id,
            html.escape(display_name_by_chat_id[chat_id]),
            message_count,
        )
        for chat_id, _chat_title, _identity_key, message_count, _last_message_at in visible_chats
    ]
    if not chat_links:
        chat_links = ['<p class="empty">Noch keine Chats importiert.</p>']

    message_html: list[str] = []
    for message, chat_title in messages:
        media_html = "".join(_render_media(item, message) for item in media_by_message.get(message.id, []))
        if not media_html and message.deleted_at is None and message.raw_data:
            try:
                raw_content = json.loads(message.raw_data).get("message") or {}
            except (TypeError, ValueError):
                raw_content = {}
            if isinstance(raw_content, dict) and "videoMessage" in raw_content:
                media_html = (
                    f'<button class="media-retry" type="button" data-message-id="{message.id}">Video erneut laden</button>'
                    f'<small class="media-retry-status" id="media-retry-{message.id}" hidden></small>'
                )
        reactions = reactions_by_message.get(message.id, [])
        reaction_html = (
            '<div class="message-reactions">{}</div>'.format(
                "".join(f'<span class="reaction">{html.escape(_display_reaction(reaction))}</span>' for reaction in reactions)
            )
            if reactions
            else ""
        )
        sender_key = message.sender or chat_display_name_by_id.get(message.chat_id, chat_title)
        sender = display_name_by_jid.get(sender_key, _display_chat_name(message.chat_id, sender_key))
        previous = versions_by_message.get(message.id, [])
        versions_html = ""
        if previous:
            versions_html = '<details class="versions"><summary>{} frühere Fassung(en)</summary>{}</details>'.format(
                len(previous), "".join(f"<p>{html.escape(version.text)}</p>" for version in previous)
            )
        deleted_html = '<small class="deleted">Auf WhatsApp gelöscht</small>' if message.deleted_at else ""
        utc_attribute = ""
        if message.source_key is not None:
            utc_value = message.sent_at.replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            utc_attribute = f' data-utc="{utc_value}"'
        message_html.append(
            '<article id="message-{message_id}" class="message{message_class}" data-open-chat="{open_chat}" data-chat-id="{chat_id}" data-message-id="{message_id}"{tabindex}><div class="message-meta"><strong>{sender}</strong><time{utc_attribute}>{time}</time></div>'
            '<p>{text}</p>{reactions}{media}{deleted}{versions}</article>'.format(
                message_id=message.id,
                message_class=" message-link" if selected_chat is None else "",
                open_chat="true" if selected_chat is None else "false",
                chat_id=message.chat_id,
                tabindex=' tabindex="0"' if selected_chat is None else "",
                sender=html.escape(_display_chat_name(message.chat_id, sender)),
                time=message.sent_at.strftime("%d.%m.%Y %H:%M"),
                utc_attribute=utc_attribute,
                text=html.escape(_display_message_text(message)),
                reactions=reaction_html,
                media=media_html,
                deleted=deleted_html,
                versions=versions_html,
            )
        )
    if not message_html:
        message_html = ['<p class="empty">Keine passenden Nachrichten.</p>']

    safe_query = html.escape(query, quote=True)
    clear_filter = '<a class="clear-filter" href="/">Alle Chats</a>' if selected_chat or query else ""
    selected_title = next(
        (resolved_chat_names.get(chat_id, _display_chat_name(chat_id, name)) for chat_id, name, _identity, _count, _last_message_at in chats if chat_id == selected_chat),
        "Alle Chats",
    )
    if selected_chat is not None:
        selected_title = _display_chat_name(selected_chat, selected_title)
    contact_name_form = ""
    if selected_chat is not None:
        contact_name_form = (
            f'<form class="contact-name-form" action="/chats/{selected_chat}/name" method="post">'
            f'<input name="name" maxlength="500" required value="{html.escape(selected_title, quote=True)}" aria-label="Anzeigename">'
            '<button type="submit">Anzeigename speichern</button></form>'
        )
    chat_sidebar = (
        f'<aside><div class="chat-sidebar-heading"><h2>Chats · {len(visible_chats)}</h2></div>'
        f'<nav class="chat-list">{"".join(chat_links)}</nav>'
        '</aside>'
    )
    conversation_class = ""
    conversation_back = ""
    search_action = f"/chats/{selected_chat}" if selected_chat is not None else "/"

    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>WhatsApp Archive</title>
  <style>
    :root {{ color-scheme: light; --ink: #17211f; --muted: #65716e; --line: #dce3df; --paper: #f4f6f2; --white: #fff; --green: #176b52; --green-soft: #e2f1e9; --coral: #bb563c; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--paper); color: var(--ink); font-family: "Avenir Next", "Segoe UI", sans-serif; }}
    header {{ min-height: 74px; display: flex; align-items: center; justify-content: space-between; padding: 16px clamp(18px, 4vw, 56px); background: var(--white); border-bottom: 1px solid var(--line); }}
    h1 {{ margin: 0; font: 600 25px/1.1 Georgia, serif; }}
    .status {{ color: var(--green); font-size: 13px; }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 28px clamp(16px, 3vw, 42px); }}
    h2 {{ margin: 0 0 14px; font-size: 15px; font-weight: 650; }}
    input, button {{ min-height: 42px; border: 1px solid var(--line); border-radius: 4px; background: var(--white); color: var(--ink); font: inherit; padding: 8px 10px; }}
    button {{ padding: 8px 16px; background: var(--green); border-color: var(--green); color: var(--white); cursor: pointer; font-weight: 600; }}
    button:hover {{ background: #0e543f; }}
    .layout {{ display: grid; grid-template-columns: minmax(220px, 290px) minmax(0, 1fr); gap: 32px; padding-top: 26px; }}
    .conversation-layout {{ grid-template-columns: minmax(0, 1fr); }}
    aside {{ border-right: 1px solid var(--line); padding-right: 22px; }}
    .chat-sidebar-heading {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .chat-sidebar-heading h2 {{ margin: 0; }}
    .chat-sidebar-heading button {{ min-height: 32px; padding: 4px 8px; font-size: 11px; }}
    .chat-list {{ display: grid; gap: 3px; }}
    .chat-link {{ display: flex; justify-content: space-between; align-items: baseline; gap: 10px; min-width: 0; padding: 11px 10px; border-radius: 3px; color: var(--ink); text-decoration: none; }}
    .chat-link span {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .chat-link small, .chat-link.selected small {{ color: var(--muted); }}
    .chat-link:hover, .chat-link.selected {{ background: var(--green-soft); color: var(--green); }}
    .feed-head {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }}
    .feed-head h2 {{ margin: 0; font: 600 21px/1.2 Georgia, serif; }}
    .feed-title {{ display: grid; gap: 8px; min-width: 0; }}
    .contact-name-form {{ display: flex; gap: 6px; max-width: 460px; }}
    .contact-name-form input {{ min-width: 0; flex: 1; min-height: 34px; }}
    .contact-name-form button {{ min-height: 34px; padding: 4px 10px; font-size: 12px; }}
    .search {{ display: flex; gap: 7px; width: min(100%, 420px); }}
    .search input {{ min-width: 0; flex: 1; }}
    .search button {{ min-height: 40px; padding: 7px 13px; }}
    .clear-filter {{ color: var(--green); font-size: 13px; white-space: nowrap; }}
    .feed {{ display: grid; gap: 0; border-top: 1px solid var(--line); }}
    .message {{ padding: 16px 4px; border-bottom: 1px solid var(--line); }}
    .message-link {{ cursor: pointer; }}
    .message:focus {{ outline: 2px solid rgba(23, 107, 82, 0.3); outline-offset: 2px; }}
    .message-meta {{ display: flex; justify-content: space-between; gap: 16px; font-size: 13px; }}
    .message-meta time {{ color: var(--muted); white-space: nowrap; }}
    .message p {{ margin: 8px 0 0; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .message-reactions {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }}
    .reaction {{ display: inline-flex; align-items: center; min-height: 24px; padding: 2px 7px; border: 1px solid var(--line); border-radius: 999px; background: var(--white); font-size: 14px; }}
    .message-reactions {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }}
    .reaction {{ display: inline-flex; align-items: center; min-height: 24px; padding: 2px 7px; border: 1px solid var(--line); border-radius: 999px; background: var(--white); font-size: 14px; }}
    .attachment {{ display: inline-block; margin: 10px 8px 0 0; color: var(--green); font-size: 13px; }}
    .media-preview {{ display: block; max-width: min(100%, 640px); margin-top: 10px; }}
    .media-preview img, .media-preview video {{ display: block; max-width: 100%; max-height: 420px; border-radius: 4px; }}
    .media-preview audio {{ display: block; width: min(100%, 420px); }}
    .media-retry {{ margin-top: 10px; }}
    .media-retry-status {{ color: var(--coral); font-size: 12px; }}
    .connection {{ display: flex; align-items: center; gap: 22px; padding: 0 0 22px; border-bottom: 1px solid var(--line); }}
    .connection-copy {{ min-width: 220px; }}
    .connection-state {{ margin: 0 0 5px; color: var(--green); font-weight: 650; }}
    .connection-counts, .connection-error {{ color: var(--muted); font-size: 12px; }}
    .connection-error, .deleted {{ display: block; margin-top: 7px; color: var(--coral); }}
    .pairing {{ display: flex; align-items: center; gap: 12px; color: var(--muted); font-size: 12px; }}
    .pairing img {{ width: 112px; height: 112px; border: 1px solid var(--line); border-radius: 4px; background: white; }}
    .pairing span {{ max-width: 260px; line-height: 1.4; }}
    .versions {{ margin-top: 10px; color: var(--muted); font-size: 12px; }}
    .versions summary {{ cursor: pointer; }}
    .versions p {{ border-left: 2px solid var(--line); padding-left: 9px; }}
    .empty {{ color: var(--muted); font-size: 13px; }}
    @media (max-width: 760px) {{ .connection {{ align-items: flex-start; flex-direction: column; gap: 10px; }} .connection-copy {{ min-width: 0; }} .pairing {{ align-items: flex-start; }} .layout {{ grid-template-columns: 1fr; gap: 22px; }} aside {{ border-right: 0; border-bottom: 1px solid var(--line); padding: 0 0 18px; }} .chat-list {{ display: flex; overflow-x: auto; padding-bottom: 4px; }} .chat-link {{ flex: 0 0 auto; max-width: 220px; }} .feed-head {{ align-items: flex-start; flex-direction: column; }} .search {{ width: 100%; }} }}
  </style>
</head>
<body>
  <header><h1>WhatsApp Archive</h1><span class="status">Lokales Archiv</span></header>
  <main>
        <section class="connection" aria-live="polite">
            <div class="connection-copy"><p class="connection-state" id="connection-state">{state_label}</p><small class="connection-counts" id="connection-counts">{archive_counts}</small>{bridge_error_html}</div>
            <div id="pairing-area">{qr_panel}</div>
        </section>
        <div class="layout{conversation_class}">
            {chat_sidebar}
      <section>
        <div class="feed-head">
                                        <div class="feed-title"><h2>{conversation_back}{html.escape(selected_title)}</h2>{contact_name_form}</div>
                    <form class="search" action="{search_action}" method="get"><input name="q" value="{safe_query}" placeholder="Nachrichten durchsuchen" aria-label="Nachrichten durchsuchen"><button type="submit">Suchen</button></form>
                    {'' if selected_chat is not None else clear_filter}
        </div>
        <div class="feed">{''.join(message_html)}</div>
      </section>
    </div>
  </main>
    <script>
        document.addEventListener('click', (event) => {{
            const target = event.target.closest('.message');
            if (!target || target.dataset.openChat !== 'true') return;
            if (event.target.closest('a, summary, details')) return;
            const chatId = target.dataset.chatId;
            const messageId = target.dataset.messageId;
            if (chatId) {{
                const url = new URL(`/chats/${{chatId}}`, window.location.origin);
                if (messageId) url.hash = `message-${{messageId}}`;
                window.location.href = url.toString();
            }}
        }});
        document.addEventListener('keydown', (event) => {{
            if (event.key !== 'Enter' && event.key !== ' ') return;
            const target = event.target.closest('.message');
            if (!target || target.dataset.openChat !== 'true') return;
            if (event.target.closest('a, summary, details')) return;
            event.preventDefault();
            target.click();
        }});
        const localDateTime = new Intl.DateTimeFormat(navigator.language, {{
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        }});
        document.querySelectorAll('time[data-utc]').forEach((element) => {{
            const instant = new Date(element.dataset.utc);
            if (!Number.isNaN(instant.getTime())) element.textContent = localDateTime.format(instant);
        }});
        const renderedLiveRevision = Number({whatsapp_status.get('live_revision', 0)});
        let previousLiveRevision = null;
        let stableLiveRevisionPolls = 0;
        async function refreshWhatsAppStatus() {{
            try {{
                const response = await fetch('/api/whatsapp/status', {{ cache: 'no-store' }});
                if (!response.ok) return;
                const state = await response.json();
                const labels = {{ bridge_unavailable: 'Bridge nicht erreichbar', starting: 'Verbindung startet', pairing_required: 'QR-Kopplung erforderlich', connected: 'Verbunden', syncing_history: 'Chatverlauf wird synchronisiert', reconnecting: 'Verbindung wird wiederhergestellt', logged_out: 'Abgemeldet', error: 'Verbindungsfehler', mock: 'Testmodus' }};
                document.getElementById('connection-state').textContent = labels[state.state] || 'Unbekannter Status';
                document.getElementById('connection-counts').textContent = `${{state.archived_chats}} Chats · ${{state.archived_messages}} Nachrichten im Archiv`;
                const liveRevision = Number(state.live_revision || 0);
                const changedSinceRender = liveRevision !== renderedLiveRevision;
                if (changedSinceRender && liveRevision === previousLiveRevision) {{
                    stableLiveRevisionPolls += 1;
                }} else {{
                    stableLiveRevisionPolls = changedSinceRender ? 1 : 0;
                }}
                previousLiveRevision = liveRevision;
                if (stableLiveRevisionPolls >= 2 && !['starting', 'syncing_history', 'reconnecting'].includes(state.state)) {{
                    window.location.reload();
                    return;
                }}
                const error = document.getElementById('connection-error');
                error.textContent = state.lastError || '';
                error.hidden = !state.lastError;
                const area = document.getElementById('pairing-area');
                if (state.qrDataUrl) {{
                    area.innerHTML = '<div class="pairing"><img alt="WhatsApp-Verknüpfungs-QR-Code"><span>In WhatsApp unter Verknüpfte Geräte scannen. Den Code nicht weitergeben.</span></div>';
                    area.querySelector('img').src = state.qrDataUrl;
                }} else if (state.state === 'logged_out') {{
                    area.innerHTML = '<div class="pairing"><button type="button" id="pairing-reset">Neue lokale Kopplung</button><span>Setzt nur die lokale Sitzung zurück und fordert einen neuen QR-Code an.</span></div>';
                }} else {{ area.replaceChildren(); }}
            }} catch {{}}
        }}
        document.addEventListener('click', async (event) => {{
            const button = event.target.closest('.media-retry');
            if (!button || button.disabled) return;
            const status = document.getElementById(`media-retry-${{button.dataset.messageId}}`);
            button.disabled = true;
            button.textContent = 'Video wird geladen …';
            try {{
                const response = await fetch(`/api/media/${{button.dataset.messageId}}/retry`, {{ method: 'POST' }});
                if (!response.ok) throw new Error('Video retry failed');
                window.location.reload();
            }} catch {{
                button.disabled = false;
                button.textContent = 'Video erneut laden';
                status.textContent = 'Video konnte nicht geladen werden. Bitte später erneut versuchen.';
                status.hidden = false;
            }}
        }});
        document.addEventListener('click', async (event) => {{
            const button = event.target.closest('#pairing-reset');
            if (!button) return;
            if (!window.confirm('Die lokale WhatsApp-Sitzung wird entfernt. Danach wird ein neuer QR-Code angefordert. Fortfahren?')) return;
            button.disabled = true;
            button.textContent = 'Kopplung wird vorbereitet …';
            try {{
                const response = await fetch('/api/whatsapp/pairing/reset', {{ method: 'POST' }});
                if (!response.ok) throw new Error('Pairing reset failed');
                await refreshWhatsAppStatus();
            }} catch {{
                button.disabled = false;
                button.textContent = 'Neue lokale Kopplung';
            }}
        }});
        window.setInterval(refreshWhatsAppStatus, 2500);
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index(
    q: str = Query(default="", max_length=200),
    chat: int | None = Query(default=None),
) -> HTMLResponse:
    if chat is None:
        with SessionFactory() as session:
            chat = session.scalar(
                select(Message.chat_id)
                .where(_chat_activity_predicate())
                .group_by(Message.chat_id)
                .order_by(func.max(Message.sent_at).desc(), Message.chat_id)
                .limit(1)
            )
    return HTMLResponse(
        _page(q, chat),
        headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
    )


@app.get("/chats/{chat_id}", response_class=HTMLResponse)
def chat_view(chat_id: int, q: str = Query(default="", max_length=200)) -> HTMLResponse:
    with SessionFactory() as session:
        if session.get(Chat, chat_id) is None:
            raise HTTPException(status_code=404, detail="Chat not found")
    return HTMLResponse(
        _page(q, chat_id),
        headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
    )


@app.post("/chats/{chat_id}/name")
def rename_chat(chat_id: int, request: FastAPIRequest, name: str = Form(..., max_length=500)) -> RedirectResponse:
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin request required")
    title = name.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Contact name is required")
    with SessionFactory.begin() as session:
        chat = session.get(Chat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        chat.title = title
    return RedirectResponse(f"/chats/{chat_id}", status_code=303)


@app.get("/media/{media_id}")
def get_media(media_id: int, download: bool = Query(default=False)) -> FileResponse:
    with SessionFactory() as session:
        media = session.get(Media, media_id)
        if media is None:
            raise HTTPException(status_code=404, detail="Media not found")
        message = session.get(Message, media.message_id)
        path = (MEDIA_DIR / media.stored_path).resolve()
        if not path.is_relative_to(MEDIA_DIR.resolve()) or not path.is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        media_type = _media_content_type(media.original_name, message.raw_data if message else None)
        media_type, disposition, headers = _media_response_options(media.original_name, media_type)
        if download:
            disposition = "attachment"
        return FileResponse(
            path,
            media_type=media_type,
            filename=media.original_name,
            content_disposition_type=disposition,
            headers=headers,
        )