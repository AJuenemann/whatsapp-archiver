"""Verified, independently restorable snapshots of the local SQLite archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_LOCK = threading.RLock()
TABLES = ("chats", "messages", "message_versions", "media", "contact_mappings", "archive_metadata")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contained_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not relative or Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("Snapshot contains an invalid file path")
    if not path.is_file():
        raise ValueError(f"Required archive file is missing: {relative}")
    return path


def _open_database(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def _inventory(database: Path) -> dict:
    with _open_database(database) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Archive database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Archive database has invalid references")
        identity = connection.execute(
            "SELECT value FROM archive_metadata WHERE key = 'archive_id'"
        ).fetchone()
        if identity is None:
            raise ValueError("Archive identity is missing")
        records = {}
        for table in TABLES:
            digest = hashlib.sha256()
            count = 0
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"):
                digest.update(json.dumps(row, ensure_ascii=True).encode("ascii") + b"\n")
                count += 1
            records[table] = {"count": count, "sha256": digest.hexdigest()}
        media = {}
        for relative, digest in connection.execute("SELECT stored_path, sha256 FROM media"):
            if relative in media and media[relative] != digest:
                raise ValueError("Conflicting archived media hashes")
            media[relative] = digest
    return {"archive_id": identity[0], "records": records, "media": media}


def verify_snapshot(snapshot: Path) -> dict:
    manifest = json.loads(contained_file(snapshot, "manifest.json").read_text("utf-8"))
    if manifest.get("format") != 1:
        raise ValueError("Unsupported snapshot format")
    database = contained_file(snapshot, "archive.db")
    if file_hash(database) != manifest["database_sha256"]:
        raise ValueError("Snapshot database checksum mismatch")
    inventory = _inventory(database)
    if inventory != manifest["inventory"]:
        raise ValueError("Snapshot inventory mismatch")
    identity = contained_file(snapshot, "archive.db.identity").read_text("ascii").strip()
    if identity != inventory["archive_id"]:
        raise ValueError("Snapshot archive identity mismatch")
    for relative, digest in inventory["media"].items():
        if file_hash(contained_file(snapshot / "media", relative)) != digest:
            raise ValueError(f"Snapshot media checksum mismatch: {relative}")
    for name, digest in manifest["pending_events"].items():
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("Invalid pending event filename")
        if file_hash(contained_file(snapshot / "whatsapp-events", name)) != digest:
            raise ValueError(f"Snapshot event checksum mismatch: {name}")
    return manifest


def _sync_files(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as source:
                os.fsync(source.fileno())
    for path in [*(item for item in root.rglob("*") if item.is_dir()), root]:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_snapshot(engine, media_root: Path, events_root: Path, backup_root: Path) -> Path:
    if engine.dialect.name != "sqlite" or not engine.url.database or engine.url.database == ":memory:":
        raise ValueError("Verified archive protection requires a file-based SQLite database")
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync_directory(backup_root.parent)
    staging = backup_root / ("." + name + ".partial")
    staging.mkdir(mode=0o700)
    try:
        with ARCHIVE_LOCK:
            event_paths = sorted(events_root.glob("*.json"))
            (staging / "whatsapp-events").mkdir()
            pending = {}
            for event in event_paths:
                destination = staging / "whatsapp-events" / event.name
                shutil.copyfile(event, destination)
                pending[event.name] = file_hash(destination)
            database = staging / "archive.db"
            source = _open_database(Path(engine.url.database))
            target = sqlite3.connect(database)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            inventory = _inventory(database)
            (staging / "archive.db.identity").write_text(inventory["archive_id"] + "\n", "ascii")
            for relative, digest in inventory["media"].items():
                source_path = contained_file(media_root, relative)
                destination = staging / "media" / relative
                if not destination.resolve().is_relative_to((staging / "media").resolve()):
                    raise ValueError("Invalid archived media path")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)
                if file_hash(destination) != digest:
                    raise ValueError(f"Archived media checksum mismatch: {relative}")
            manifest = {
                "format": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database_sha256": file_hash(database),
                "inventory": inventory,
                "pending_events": pending,
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=True), "utf-8")
            verify_snapshot(staging)
            _sync_files(staging)
            destination = backup_root / name
            staging.rename(destination)
            _sync_directory(backup_root)
            return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def restore_snapshot(snapshot: Path, destination: Path) -> None:
    manifest = verify_snapshot(snapshot)
    if destination.exists():
        raise ValueError("Restore destination already exists; existing archives are never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / ("." + destination.name + "-" + uuid.uuid4().hex + ".partial")
    staging.mkdir(mode=0o700)
    try:
        for name in ("archive.db", "archive.db.identity"):
            shutil.copyfile(snapshot / name, staging / name)
        for directory in ("media", "whatsapp-events"):
            if (snapshot / directory).exists():
                shutil.copytree(snapshot / directory, staging / directory)
        shutil.copyfile(snapshot / "manifest.json", staging / "manifest.json")
        if verify_snapshot(staging) != manifest:
            raise ValueError("Restored archive verification failed")
        _sync_files(staging)
        staging.rename(destination)
        _sync_directory(destination.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("verify", "restore"))
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.operation == "restore":
        if args.destination is None:
            parser.error("restore requires --destination (a new, nonexistent directory)")
        restore_snapshot(args.snapshot, args.destination)
    else:
        verify_snapshot(args.snapshot)
    print("Archive snapshot verified successfully")


if __name__ == "__main__":
    main()
