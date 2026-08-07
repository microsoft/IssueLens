"""Normalize protocol media inputs into GitHub Copilot SDK attachments."""

from __future__ import annotations

import base64
import binascii
import mimetypes
from collections.abc import Sequence
from typing import Any

from azure.ai.agentserver.responses.models import (
    ItemMessage,
    MessageContentInputFileContent,
    MessageContentInputImageContent,
    MessageContentInputTextContent,
)
from copilot.session import Attachment


MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024


class MediaInputError(ValueError):
    """Raised when a protocol media input cannot be safely forwarded."""


def invocation_attachments(value: Any) -> list[Attachment]:
    """Validate invocation ``blob`` attachments for ``session.send``."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise MediaInputError('"attachments" must be an array')
    if len(value) > MAX_ATTACHMENTS:
        raise MediaInputError(
            f"at most {MAX_ATTACHMENTS} attachments are allowed"
        )

    attachments: list[Attachment] = []
    total_bytes = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise MediaInputError(f"attachment {index} must be an object")
        attachment_type = item.get("type")
        if attachment_type == "file":
            raise MediaInputError(
                'invocation attachments cannot use type "file"; send an '
                'inline type "blob" attachment instead'
            )
        if attachment_type != "blob":
            raise MediaInputError(
                f'attachment {index} has unsupported type "{attachment_type}"'
            )

        data = item.get("data")
        mime_type = item.get("mimeType")
        display_name = item.get("displayName")
        if not isinstance(data, str) or not data:
            raise MediaInputError(f"attachment {index} requires base64 data")
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise MediaInputError(f"attachment {index} requires mimeType")
        if display_name is not None and not isinstance(display_name, str):
            raise MediaInputError(
                f"attachment {index} displayName must be a string"
            )

        attachment, byte_count = _blob_attachment(
            data,
            mime_type=mime_type,
            display_name=display_name,
            source=f"attachment {index}",
        )
        total_bytes = _add_to_total(total_bytes, byte_count)
        attachments.append(attachment)
    return attachments


def response_input(items: Sequence[Any]) -> tuple[str, list[Attachment]]:
    """Extract text and inline image/file attachments from Responses items."""
    texts: list[str] = []
    attachments: list[Attachment] = []
    total_bytes = 0

    for item in items:
        if not isinstance(item, ItemMessage):
            continue
        content = item.content
        if isinstance(content, str):
            if content:
                texts.append(content)
            continue

        for part in content:
            if isinstance(part, MessageContentInputTextContent):
                if part.text:
                    texts.append(part.text)
                continue

            if len(attachments) >= MAX_ATTACHMENTS:
                raise MediaInputError(
                    f"at most {MAX_ATTACHMENTS} attachments are allowed"
                )

            if isinstance(part, MessageContentInputImageContent):
                if part.file_id or not part.image_url:
                    raise MediaInputError(
                        "input_image requires an inline base64 data URL; "
                        "file_id is not supported"
                    )
                if not part.image_url.startswith("data:"):
                    raise MediaInputError(
                        "remote input_image URLs are not supported; send a "
                        "base64 data URL"
                    )
                attachment, byte_count = _blob_attachment(
                    part.image_url,
                    mime_type=None,
                    display_name=_image_name(part.image_url, len(attachments)),
                    source="input_image",
                    require_image=True,
                )
            elif isinstance(part, MessageContentInputFileContent):
                if part.file_id or part.file_url or not part.file_data:
                    raise MediaInputError(
                        "input_file requires inline base64 file_data; file_id "
                        "and file_url are not supported"
                    )
                mime_type = (
                    None
                    if part.file_data.startswith("data:")
                    else mimetypes.guess_type(part.filename or "")[0]
                    or "application/octet-stream"
                )
                attachment, byte_count = _blob_attachment(
                    part.file_data,
                    mime_type=mime_type,
                    display_name=part.filename or f"attachment-{len(attachments) + 1}",
                    source="input_file",
                )
            else:
                continue

            total_bytes = _add_to_total(total_bytes, byte_count)
            attachments.append(attachment)

    return "\n".join(texts), attachments


def redacted_input_items(items: Sequence[Any]) -> list[Any]:
    """Serialize Responses input items without logging inline binary data."""
    serialized = [item.as_dict() if hasattr(item, "as_dict") else str(item) for item in items]
    return [_redact(item) for item in serialized]


def _blob_attachment(
    value: str,
    *,
    mime_type: str | None,
    display_name: str | None,
    source: str,
    require_image: bool = False,
) -> tuple[Attachment, int]:
    encoded = value
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator or not header.lower().endswith(";base64"):
            raise MediaInputError(f"{source} must use a base64 data URL")
        data_mime_type = header[5:].split(";", 1)[0].strip()
        if not data_mime_type:
            raise MediaInputError(f"{source} data URL requires a media type")
        mime_type = data_mime_type

    if not mime_type:
        raise MediaInputError(f"{source} requires a media type")
    mime_type = mime_type.strip().lower()
    if require_image and not mime_type.startswith("image/"):
        raise MediaInputError(f"{source} must use an image media type")

    compact_data = "".join(encoded.split())
    maximum_encoded = ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4
    if len(compact_data) > maximum_encoded:
        raise MediaInputError(
            f"{source} exceeds the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit"
        )
    try:
        decoded = base64.b64decode(compact_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaInputError(f"{source} contains invalid base64 data") from exc
    if not decoded:
        raise MediaInputError(f"{source} cannot be empty")
    if len(decoded) > MAX_ATTACHMENT_BYTES:
        raise MediaInputError(
            f"{source} exceeds the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit"
        )

    attachment: Attachment = {
        "type": "blob",
        "data": base64.b64encode(decoded).decode("ascii"),
        "mimeType": mime_type,
    }
    if display_name:
        attachment["displayName"] = display_name
    return attachment, len(decoded)


def _add_to_total(total_bytes: int, byte_count: int) -> int:
    total_bytes += byte_count
    if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
        raise MediaInputError(
            "combined attachments exceed the "
            f"{MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB limit"
        )
    return total_bytes


def _image_name(data_url: str, index: int) -> str:
    mime_type = data_url[5:].split(";", 1)[0].lower()
    extension = mimetypes.guess_extension(mime_type) or ".img"
    return f"image-{index + 1}{extension}"


def _redact(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, dict):
        return value

    result = {}
    for key, item in value.items():
        if key in {"data", "file_data"} and isinstance(item, str):
            result[key] = "<redacted>"
        elif key == "image_url" and isinstance(item, str) and item.startswith("data:"):
            result[key] = "<redacted data URL>"
        else:
            result[key] = _redact(item)
    return result
