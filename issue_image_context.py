"""Resolve explicit GitHub issue references into Copilot image attachments."""

from __future__ import annotations

import re
from typing import Any

from copilot.session import Attachment


_ISSUE_URL_PATTERN = re.compile(
    r"https://github\.com/([A-Za-z0-9-]{1,39})/"
    r"([A-Za-z0-9_.-]{1,100})/issues/([1-9][0-9]*)"
)
_ISSUE_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9-]{1,39})/"
    r"([A-Za-z0-9_.-]{1,100})#([1-9][0-9]*)"
)
_IMAGE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def issue_references(prompt: str) -> list[tuple[str, int]]:
    """Return unique explicit GitHub issue references in prompt order."""
    matches = [
        (match.start(), f"{match.group(1)}/{match.group(2)}", int(match.group(3)))
        for pattern in (_ISSUE_URL_PATTERN, _ISSUE_REFERENCE_PATTERN)
        for match in pattern.finditer(prompt)
    ]
    references: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for _, repository, issue_number in sorted(matches):
        reference = (repository, issue_number)
        normalized = (repository.lower(), issue_number)
        if normalized in seen:
            continue
        seen.add(normalized)
        references.append(reference)
    return references


async def issue_image_attachments(
    prompt: str,
    github_client: Any,
    *,
    maximum_images: int = 5,
) -> list[Attachment]:
    """Load issue-body images for explicit references as Copilot blobs."""
    if maximum_images <= 0:
        return []
    attachments: list[Attachment] = []
    for repository, issue_number in issue_references(prompt):
        result = await github_client.execute(
            "get-issue-images",
            repository,
            issue_number=issue_number,
        )
        for image_index, image in enumerate(result.get("images", []), start=1):
            media_type = image["mime_type"]
            extension = _IMAGE_EXTENSIONS[media_type]
            attachments.append({
                "type": "blob",
                "data": image["data"],
                "mimeType": media_type,
                "displayName": (
                    f"{repository.replace('/', '-')}-{issue_number}-"
                    f"image-{image_index}{extension}"
                ),
            })
            if len(attachments) >= maximum_images:
                return attachments
    return attachments
