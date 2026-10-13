"""Media metadata shared by rendering and retry validation."""

MEDIA_LABELS = {
    "imageMessage": "Bild", "videoMessage": "Video", "audioMessage": "Audio",
    "documentMessage": "Dokument", "stickerMessage": "Sticker",
    "ptvMessage": "Video-Nachricht", "extendedTextMessage": "Link-Vorschaubild",
}
WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
            "viewOnceMessageV2Extension", "documentWithCaptionMessage",
            "associatedChildMessage", "lottieStickerMessage")


def media_content(raw_data: dict) -> dict | None:
    content = raw_data.get("message")
    for _ in range(8):
        if not isinstance(content, dict):
            return None
        wrapper = next((name for name in WRAPPERS if isinstance(content.get(name), dict)), None)
        if wrapper is None:
            return content
        content = content[wrapper].get("message")
    return None


def media_kind(raw_data: dict) -> str | None:
    content = media_content(raw_data)
    if content is None:
        return None
    for kind in MEDIA_LABELS:
        if isinstance(content.get(kind), dict):
            if kind == "extendedTextMessage" and not content[kind].get("thumbnailDirectPath"):
                continue
            return kind
    return None


def media_parts(raw_data: dict) -> dict[str, dict]:
    """Inventory downloadable files, including quotations and link favicons."""
    parts: dict[str, dict] = {}

    def visit(value: object, path: str, depth: int) -> None:
        if depth > 32:
            raise ValueError("Media metadata nesting exceeds supported limit")
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else key
            if not isinstance(item, dict):
                continue
            if key in MEDIA_LABELS or key == "faviconMMSMetadata":
                if item.get("mediaKey") and (item.get("url") or item.get("directPath") or
                                           item.get("thumbnailDirectPath")):
                    parts[child_path] = item
            visit(item, child_path, depth + 1)

    visit(raw_data.get("message"), "message", 0)
    return parts
