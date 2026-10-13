"""Idempotent import of WhatsApp text exports and ZIP attachments."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from sqlalchemy import select

from .models import Chat, ImportRecord, Media, Message, MessageVersion
from .parser import ParseResult, parse_export

MAX_IMPORT_BYTES = 500 * 1024 * 1024
MAX_TEXT_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEDIA_FILE_BYTES = 200 * 1024 * 1024
_IMPORT_LOCK = threading.Lock()
_SCAN_LOCK = threading.Lock()
_SCAN_STATE: dict[Path, tuple[tuple[int, int, int, int], bool, float]] = {}
SCAN_RETRY_DELAY_SECONDS = 30
_EVENT_RETRY_AFTER: dict[Path, float] = {}


@dataclass(frozen=True)
class ImportOutcome:
    status: str
    messages_added: int
    media_added: int
    warnings: tuple[str, ...] = ()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError("Export text is not UTF-8 or UTF-16")


def _safe_zip_entries(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, dict[str, zipfile.ZipInfo], list[str]]:
    total_size = 0
    text_files: list[zipfile.ZipInfo] = []
    attachments: dict[str, zipfile.ZipInfo] = {}
    warnings: list[str] = []

    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\x00" in name:
            raise ValueError("ZIP contains an unsafe path")
        if info.is_dir():
            continue
        total_size += info.file_size
        if total_size > MAX_ARCHIVE_BYTES:
            raise ValueError("ZIP expands beyond the supported size limit")
        if info.file_size > MAX_IMPORT_BYTES:
            raise ValueError("A ZIP member exceeds the supported size limit")
        if path.suffix.lower() == ".txt":
            text_files.append(info)
        else:
            basename = path.name
            if basename in attachments:
                warnings.append("ZIP contains repeated attachment filenames; the first copy is used")
            else:
                attachments[basename] = info

    if len(text_files) != 1:
        raise ValueError("ZIP must contain exactly one .txt chat export")
    if text_files[0].file_size > MAX_TEXT_BYTES:
        raise ValueError("Chat text exceeds the supported size limit")
    return text_files[0], attachments, warnings


def _identity(value: str) -> str:
    return hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()


def _message_identity(sent_at: str, sender: str | None, text: str, occurrence: int) -> str:
    value = "\0".join((sent_at, sender or "", text, str(occurrence)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_media_name(name: str) -> str:
    basename = PurePosixPath(name.replace("\\", "/")).name
    basename = re.sub(r"[^A-Za-z0-9._-]+", "_", basename).strip("._")
    return (basename or "attachment")[:120]


def _store_media(archive: zipfile.ZipFile, info: zipfile.ZipInfo, media_root: Path) -> tuple[str, str]:
    if info.file_size > MAX_MEDIA_FILE_BYTES:
        raise ValueError("Attachment exceeds the supported size limit")

    digest = hashlib.sha256()
    size = 0
    media_root.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=media_root, prefix=".import-", delete=False) as target:
            temporary = Path(target.name)
            with archive.open(info) as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_MEDIA_FILE_BYTES:
                        raise ValueError("Attachment exceeds the supported size limit")
                    digest.update(chunk)
                    target.write(chunk)

        sha256 = digest.hexdigest()
        relative_path = Path(sha256[:2]) / f"{sha256}_{_safe_media_name(info.filename)}"
        destination = media_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            temporary.unlink()
        else:
            os.replace(temporary, destination)
        return sha256, relative_path.as_posix()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_source(path: Path):
    if path.suffix.lower() == ".txt":
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise ValueError("Chat text exceeds the supported size limit")
        return _decode_text(path.read_bytes()), None, {}, []
    if path.suffix.lower() != ".zip":
        raise ValueError("Only .txt and .zip exports are supported")
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("Export file exceeds the supported size limit")

    archive = zipfile.ZipFile(path)
    try:
        text_info, attachments, warnings = _safe_zip_entries(archive)
        text = _decode_text(archive.read(text_info))
    except Exception:
        archive.close()
        raise
    return text, archive, attachments, warnings


def import_export(
    path: Path,
    title: str,
    date_order: str,
    session_factory: Callable,
    media_root: Path,
    original_name: str | None = None,
) -> ImportOutcome:
    if not title.strip():
        raise ValueError("Chat name is required")
    if date_order not in {"day-first", "month-first"}:
        raise ValueError("Choose day-first or month-first date order")
    if not path.is_file() or path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("Export file is missing or exceeds the supported size limit")

    with _IMPORT_LOCK:
        raw_hash = _file_hash(path)
        source_hash = hashlib.sha256(f"{raw_hash}\0{_identity(title)}\0{date_order}".encode("utf-8")).hexdigest()
        with session_factory() as session:
            previous = session.scalar(select(ImportRecord).where(ImportRecord.source_hash == source_hash))
            if previous is not None and previous.status == "complete":
                return ImportOutcome("already-imported", previous.messages_added, previous.media_added)
            if previous is None:
                previous = ImportRecord(
                    source_hash=source_hash,
                    original_name=(original_name or path.name)[:255],
                    status="processing",
                )
                session.add(previous)
            else:
                previous.status = "processing"
                previous.error_code = None
            session.commit()
            import_id = previous.id

        archive = None
        try:
            text, archive, attachments, warnings = _read_source(path)
            parsed: ParseResult = parse_export(text, date_order=date_order)
            warnings.extend(parsed.warnings)
            if not parsed.messages:
                raise ValueError("No timestamped messages were found in this export")

            message_count = 0
            media_count = 0
            identity_key = _identity(title)
            with session_factory.begin() as session:
                chat = session.scalar(select(Chat).where(Chat.identity_key == identity_key))
                if chat is None:
                    chat = Chat(identity_key=identity_key, title=title.strip())
                    session.add(chat)
                    session.flush()
                chat_id = chat.id

                existing_keys = set(session.scalars(select(Message.identity_key).where(Message.chat_id == chat_id)))
                occurrences: dict[str, int] = {}
                for exported in parsed.messages:
                    sent_at = exported.timestamp.isoformat(sep=" ")
                    base = "\0".join((sent_at, exported.sender or "", exported.text))
                    occurrence = occurrences.get(base, 0)
                    occurrences[base] = occurrence + 1
                    message_key = _message_identity(sent_at, exported.sender, exported.text, occurrence)
                    message_id = None
                    if message_key not in existing_keys:
                        message = Message(
                            chat_id=chat_id,
                            identity_key=message_key,
                            sent_at=exported.timestamp,
                            sender=exported.sender,
                            text=exported.text,
                        )
                        session.add(message)
                        session.flush()
                        message_id = message.id
                        existing_keys.add(message_key)
                        message_count += 1
                    elif archive is not None:
                        message_id = session.scalar(
                            select(Message.id).where(
                                Message.chat_id == chat_id,
                                Message.identity_key == message_key,
                            )
                        )

                    if archive is None or message_id is None:
                        continue
                    for name, info in attachments.items():
                        if name not in exported.text:
                            continue
                        if info.file_size > MAX_MEDIA_FILE_BYTES:
                            warning = "Skipped an attachment exceeding the 200 MiB size limit"
                            if warning not in warnings:
                                warnings.append(warning)
                            continue
                        sha256, stored_path = _store_media(archive, info, media_root)
                        exists = session.scalar(
                            select(Media.id).where(Media.message_id == message_id, Media.sha256 == sha256)
                        )
                        if exists is None:
                            session.add(
                                Media(
                                    message_id=message_id,
                                    original_name=_safe_media_name(name),
                                    sha256=sha256,
                                    stored_path=stored_path,
                                )
                            )
                            media_count += 1

                record = session.get(ImportRecord, import_id)
                record.chat_id = chat_id
                record.status = "complete"
                record.messages_added = message_count
                record.media_added = media_count
                record.warnings = json.dumps(warnings, ensure_ascii=True)

            return ImportOutcome("complete", message_count, media_count, tuple(warnings))
        except Exception as exc:
            with session_factory.begin() as session:
                record = session.get(ImportRecord, import_id)
                if record is not None:
                    record.status = "error"
                    record.error_code = type(exc).__name__
            raise
        finally:
            if archive is not None:
                archive.close()


def _default_title(path: Path) -> str:
    title = path.stem
    title = re.sub(r"^[0-9a-f]{12}-", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^WhatsApp Chat with\s+", "", title, flags=re.IGNORECASE)
    return title.strip() or "Imported chat"


def _scan_signature(path: Path, metadata_path: Path) -> tuple[int, int, int, int]:
    source_stat = path.stat()
    if metadata_path.exists():
        metadata_stat = metadata_path.stat()
        return source_stat.st_size, source_stat.st_mtime_ns, metadata_stat.st_size, metadata_stat.st_mtime_ns
    return source_stat.st_size, source_stat.st_mtime_ns, 0, 0


def scan_incoming(incoming: Path, session_factory: Callable, media_root: Path) -> None:
    incoming.mkdir(parents=True, exist_ok=True)
    active_paths: set[Path] = set()
    for path in sorted(incoming.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".txt", ".zip"}:
            continue
        active_paths.add(path)
        metadata_path = path.with_suffix(path.suffix + ".json")
        signature = _scan_signature(path, metadata_path)
        with _SCAN_LOCK:
            previous = _SCAN_STATE.get(path)
            if previous is None or previous[0] != signature:
                _SCAN_STATE[path] = (signature, False, 0.0)
                continue
            if previous[1] or time.monotonic() < previous[2]:
                continue
        try:
            metadata = json.loads(metadata_path.read_text("utf-8")) if metadata_path.exists() else {}
            title = str(metadata.get("title") or _default_title(path))
            date_order = str(metadata.get("date_order") or "day-first")
            import_export(
                path,
                title,
                date_order,
                session_factory,
                media_root,
                original_name=metadata.get("original_name"),
            )
        except Exception:
            # ImportRecord holds the error class; never log chat titles or message text.
            with _SCAN_LOCK:
                _SCAN_STATE[path] = (signature, False, time.monotonic() + SCAN_RETRY_DELAY_SECONDS)
            continue
        with _SCAN_LOCK:
            _SCAN_STATE[path] = (signature, True, 0.0)
    with _SCAN_LOCK:
        for path in set(_SCAN_STATE) - active_paths:
            del _SCAN_STATE[path]


def remember_upload(path: Path) -> None:
    signature = _scan_signature(path, path.with_suffix(path.suffix + ".json"))
    with _SCAN_LOCK:
        _SCAN_STATE[path] = (signature, True, 0.0)


def ingest_whatsapp_event(event: dict, session_factory: Callable, media_root: Path) -> None:
    event_type = event.get("type")
    jid = event.get("jid")
    if event_type not in {"chat", "message", "delete"} or not isinstance(jid, str) or not jid:
        raise ValueError("Invalid WhatsApp event")

    chat_key = _identity(jid)
    title = str(event.get("title") or jid).strip()[:500] or jid
    archived = event.get("isArchived") if isinstance(event.get("isArchived"), bool) else None
    with session_factory.begin() as session:
        chat = session.scalar(select(Chat).where(Chat.identity_key == chat_key))
        if chat is None:
            chat = Chat(identity_key=chat_key, title=title, is_archived=archived)
            session.add(chat)
            session.flush()
        elif title != jid and chat.title != title:
            chat.title = title
        if archived is not None:
            chat.is_archived = archived

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
                    identity_key=hashlib.sha256(f"whatsapp:{source_key}".encode("utf-8")).hexdigest(),
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
                identity_key=hashlib.sha256(f"whatsapp:{source_key}".encode("utf-8")).hexdigest(),
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


def scan_whatsapp_events(events_root: Path, session_factory: Callable, media_root: Path) -> None:
    events_root.mkdir(parents=True, exist_ok=True)
    now = time.monotonic()
    for path in sorted(events_root.glob("*.json")):
        if _EVENT_RETRY_AFTER.get(path, 0) > now:
            continue
        try:
            event = json.loads(path.read_text("utf-8"))
            ingest_whatsapp_event(event, session_factory, media_root)
            path.unlink()
            _EVENT_RETRY_AFTER.pop(path, None)
        except Exception:
            _EVENT_RETRY_AFTER[path] = time.monotonic() + SCAN_RETRY_DELAY_SECONDS


def save_upload(upload, title: str, date_order: str, incoming: Path) -> Path:
    incoming.mkdir(parents=True, exist_ok=True)
    name = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if suffix not in {".txt", ".zip"}:
        raise ValueError("Choose a WhatsApp .txt or .zip export")
    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=incoming, prefix=".upload-", suffix=".part", delete=False) as target:
        temporary = Path(target.name)
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_IMPORT_BYTES:
                temporary.unlink(missing_ok=True)
                raise ValueError("Export file exceeds the 500 MiB limit")
            digest.update(chunk)
            target.write(chunk)

    source_hash = digest.hexdigest()
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._")[:100] or "export"
    destination = incoming / f"{source_hash[:12]}-{safe_stem}{suffix}"
    metadata_path = destination.with_suffix(destination.suffix + ".json")
    metadata_temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=incoming, prefix=".metadata-", suffix=".part", delete=False
        ) as metadata_file:
            metadata_temp = Path(metadata_file.name)
            json.dump(
                {"title": title.strip(), "date_order": date_order, "original_name": name},
                metadata_file,
            )
        os.replace(metadata_temp, metadata_path)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        if metadata_temp is not None:
            metadata_temp.unlink(missing_ok=True)
        raise
    return destination