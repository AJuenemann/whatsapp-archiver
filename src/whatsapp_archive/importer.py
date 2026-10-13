"""Idempotent ingestion of WhatsApp bridge events."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .models import Chat, Media, Message, MessageVersion

SCAN_RETRY_DELAY_SECONDS = 30
_EVENT_RETRY_AFTER: dict[Path, float] = {}


def _identity(value: str) -> str:
    return hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()


def _safe_media_name(name: str) -> str:
    basename = PurePosixPath(name.replace("\\", "/")).name
    basename = re.sub(r"[^A-Za-z0-9._-]+", "_", basename).strip("._")
    return (basename or "attachment")[:120]


def ingest_whatsapp_event(event: dict, session_factory: Callable, media_root: Path) -> None:
    event_type = event.get("type")
    jid = event.get("jid")
    if event_type not in {"chat", "message", "delete"} or not isinstance(jid, str) or not jid:
        raise ValueError("Invalid WhatsApp event")

    chat_key = _identity(jid)
    title = str(event.get("title") or jid).strip()[:500] or jid
    with session_factory.begin() as session:
        chat = session.scalar(select(Chat).where(Chat.identity_key == chat_key))
        if chat is None:
            chat = Chat(identity_key=chat_key, title=title)
            session.add(chat)
            session.flush()
        elif title != jid and chat.title != title:
            chat.title = title
        if event_type == "chat":
            return

        source_key = str(event.get("sourceId") or "").strip()
        if not source_key or len(source_key) > 255:
            raise ValueError("WhatsApp message has no valid source id")
        message = session.scalar(
            select(Message).where(Message.chat_id == chat.id, Message.source_key == source_key)
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if event_type == "delete":
            if message is None:
                timestamp_value = event.get("timestamp")
                try:
                    sent_at = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
                    if sent_at.tzinfo is not None:
                        sent_at = sent_at.astimezone(timezone.utc).replace(tzinfo=None)
                except (TypeError, ValueError):
                    sent_at = now
                message = Message(
                    chat_id=chat.id,
                    identity_key=hashlib.sha256(f"whatsapp:{source_key}".encode()).hexdigest(),
                    source_key=source_key,
                    sent_at=sent_at,
                    sender=str(event.get("sender") or jid)[:500],
                    text="[Nachricht gelöscht]",
                    deleted_at=now,
                )
                session.add(message)
            elif message.deleted_at is None:
                session.add(MessageVersion(message_id=message.id, text=message.text, raw_data=message.raw_data))
                message.deleted_at = now
            return

        text = str(event.get("text") or "")
        raw_data = json.dumps(event.get("rawData"), ensure_ascii=True) if event.get("rawData") is not None else None
        timestamp = datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        if message is None:
            message = Message(
                chat_id=chat.id,
                identity_key=hashlib.sha256(f"whatsapp:{source_key}".encode()).hexdigest(),
                source_key=source_key,
                sent_at=timestamp,
                sender=str(event.get("sender") or jid)[:500],
                text=text,
                raw_data=raw_data,
            )
            session.add(message)
            session.flush()
        elif message.text != text or message.raw_data != raw_data:
            if message.text != "[Nachricht gelöscht]":
                session.add(MessageVersion(message_id=message.id, text=message.text, raw_data=message.raw_data))
            message.text = text
            message.raw_data = raw_data

        media = event.get("media")
        if isinstance(media, dict):
            relative_path = Path(str(media.get("path") or ""))
            media_path = (media_root / relative_path).resolve()
            if (
                media_path.is_relative_to(media_root.resolve())
                and media_path.is_file()
                and len(str(media.get("sha256") or "")) == 64
            ):
                exists = session.scalar(
                    select(Media.id).where(Media.message_id == message.id, Media.sha256 == media["sha256"])
                )
                if exists is None:
                    session.add(
                        Media(
                            message_id=message.id,
                            original_name=_safe_media_name(str(media.get("name") or "attachment")),
                            sha256=media["sha256"],
                            stored_path=relative_path.as_posix(),
                        )
                    )


def scan_whatsapp_events(events_root: Path, session_factory: Callable, media_root: Path) -> int:
    events_root.mkdir(parents=True, exist_ok=True)
    now = time.monotonic()
    processed = 0
    for path in sorted(events_root.glob("*.json")):
        if _EVENT_RETRY_AFTER.get(path, 0) > now:
            continue
        try:
            event = json.loads(path.read_text("utf-8"))
            ingest_whatsapp_event(event, session_factory, media_root)
            path.unlink()
            _EVENT_RETRY_AFTER.pop(path, None)
            processed += 1
        except (OSError, ValueError, TypeError, SQLAlchemyError):
            _EVENT_RETRY_AFTER[path] = time.monotonic() + SCAN_RETRY_DELAY_SECONDS
    return processed
