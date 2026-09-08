"""Load validated, capability-scoped IssueLens repository instructions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import yaml


CONFIG_DIRECTORY = ".github"
CONFIG_FILENAME = "issuelens.yml"
CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 16 * 1024
MAX_INSTRUCTION_BYTES = 64 * 1024

INSTRUCTION_DOMAINS = frozenset({
    "assignment",
    "criticality",
    "duplicate_detection",
    "labeling",
    "notification_content",
    "planning",
    "team_memory",
})

LEGACY_INSTRUCTION_PATHS: dict[str, tuple[str, ...]] = {
    "assignment": (
        ".github/area_owners.md",
        "docs/area_owners.md",
        "area_owners.md",
    ),
    "labeling": (".github/label-instructions.md",),
}


class IssueLensConfigError(RuntimeError):
    """Raised when repository configuration is present but unusable."""


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: Any, deep: bool = False) -> dict[str, Any]:
        if not isinstance(node, yaml.nodes.MappingNode):
            raise IssueLensConfigError("YAML mappings must use key/value entries")
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise IssueLensConfigError("YAML mapping keys must be strings")
            if key in mapping:
                raise IssueLensConfigError(f"Duplicate YAML key: {key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def validate_wiki_repository(value: Any) -> str:
    """Validate a GitHub parent repository identifier, not a URL or Git remote."""
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"
            r"/[A-Za-z0-9._-]{1,100}",
            value,
        ) is None
        or value.rsplit("/", 1)[-1] in {".", ".."}
        or value.casefold().endswith(".git")
    ):
        raise IssueLensConfigError(
            "wiki_repository must be a GitHub owner/repository identifier "
            "(owner up to 39 characters, repository up to 100), not a URL or "
            "a .git/.wiki.git remote"
        )
    return value


def _is_not_found(error: Exception) -> bool:
    return (
        getattr(error, "status_code", None) == 404
        or "HTTP 404" in str(error)
    )


def _validate_instruction_path(value: Any, domain: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise IssueLensConfigError(
            f"instructions.{domain}.path must be a non-empty string"
        )
    if len(value) > 240:
        raise IssueLensConfigError(
            f"instructions.{domain}.path exceeds 240 characters"
        )
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise IssueLensConfigError(
            f"instructions.{domain}.path must be a repository-relative POSIX path"
        ) from error
    segments = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise IssueLensConfigError(
            f"instructions.{domain}.path must be a repository-relative POSIX path"
        )
    if not value.casefold().endswith(".md"):
        raise IssueLensConfigError(
            f"instructions.{domain}.path must reference a Markdown file"
        )
    return value


def parse_config(content: str) -> dict[str, str]:
    """Parse the versioned IssueLens config into domain-to-path mappings."""
    return {domain: entry["path"] for domain, entry in _parse_config(content).items()}


def _parse_config(content: str) -> dict[str, dict[str, str]]:
    if not isinstance(content, str):
        raise IssueLensConfigError(f"{CONFIG_FILENAME} must be a UTF-8 text string")
    if len(content.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise IssueLensConfigError(
            f"{CONFIG_FILENAME} exceeds {MAX_CONFIG_BYTES} bytes"
        )
    try:
        tokens = tuple(yaml.scan(content, Loader=yaml.SafeLoader))
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in tokens
        ):
            raise IssueLensConfigError("YAML anchors and aliases are not supported")
        documents = list(yaml.load_all(content, Loader=_UniqueKeyLoader))
    except yaml.YAMLError as error:
        raise IssueLensConfigError(f"Invalid {CONFIG_FILENAME}: {error}") from error

    if len(documents) != 1 or not isinstance(documents[0], Mapping):
        raise IssueLensConfigError(
            f"{CONFIG_FILENAME} must contain exactly one YAML mapping"
        )
    config = documents[0]
    unknown_root_keys = set(config) - {"version", "instructions"}
    if unknown_root_keys:
        names = ", ".join(sorted(str(key) for key in unknown_root_keys))
        raise IssueLensConfigError(f"Unknown {CONFIG_FILENAME} keys: {names}")
    if type(config.get("version")) is not int or config["version"] != CONFIG_VERSION:
        raise IssueLensConfigError(
            f"{CONFIG_FILENAME} version must be the integer {CONFIG_VERSION}"
        )

    instructions = config.get("instructions", {})
    if not isinstance(instructions, Mapping):
        raise IssueLensConfigError("instructions must be a YAML mapping")
    unknown_domains = set(instructions) - INSTRUCTION_DOMAINS
    if unknown_domains:
        names = ", ".join(sorted(str(key) for key in unknown_domains))
        raise IssueLensConfigError(f"Unknown instruction domains: {names}")

    entries: dict[str, dict[str, str]] = {}
    for domain, entry in instructions.items():
        if not isinstance(entry, Mapping):
            raise IssueLensConfigError(f"instructions.{domain} must be a mapping")
        allowed_keys = {"path", "wiki_repository"} if domain == "team_memory" else {"path"}
        unknown_entry_keys = set(entry) - allowed_keys
        if unknown_entry_keys:
            names = ", ".join(sorted(str(key) for key in unknown_entry_keys))
            raise IssueLensConfigError(
                f"Unknown instructions.{domain} keys: {names}"
            )
        if "path" not in entry:
            raise IssueLensConfigError(f"instructions.{domain}.path is required")
        validated = {"path": _validate_instruction_path(entry["path"], domain)}
        if "wiki_repository" in entry:
            validated["wiki_repository"] = validate_wiki_repository(entry["wiki_repository"])
        entries[domain] = validated
    return entries


async def _read_text(
    client: Any,
    repository: str,
    path: str,
    *,
    maximum_bytes: int,
) -> str:
    payload = await client.get_file(repository, path)
    if not isinstance(payload, Mapping):
        raise IssueLensConfigError(f"{path} is not a file")
    content = payload.get("decoded_content")
    if not isinstance(content, str):
        raise IssueLensConfigError(f"{path} is not a UTF-8 text file")
    if len(content.encode("utf-8")) > maximum_bytes:
        raise IssueLensConfigError(f"{path} exceeds {maximum_bytes} bytes")
    return content


async def _discover_config(
    client: Any,
    repository: str,
) -> tuple[str, dict[str, dict[str, str]]] | None:
    try:
        entries = await client.get_file(repository, CONFIG_DIRECTORY)
    except Exception as error:
        if _is_not_found(error):
            return None
        raise IssueLensConfigError(
            f"Could not inspect {CONFIG_DIRECTORY}: {error}"
        ) from error
    if not isinstance(entries, list):
        raise IssueLensConfigError(f"{CONFIG_DIRECTORY} is not a directory")

    matches = [
        entry
        for entry in entries
        if isinstance(entry, Mapping)
        and isinstance(entry.get("name"), str)
        and entry["name"].casefold() == CONFIG_FILENAME.casefold()
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise IssueLensConfigError(
            f"Multiple case-insensitive matches for {CONFIG_DIRECTORY}/{CONFIG_FILENAME}"
        )
    entry = matches[0]
    if entry.get("type") != "file":
        raise IssueLensConfigError(
            f"{CONFIG_DIRECTORY}/{entry['name']} is not a file"
        )
    config_path = f"{CONFIG_DIRECTORY}/{entry['name']}"
    content = await _read_text(
        client,
        repository,
        config_path,
        maximum_bytes=MAX_CONFIG_BYTES,
    )
    return config_path, _parse_config(content)


async def load_instruction(
    client: Any,
    repository: str,
    domain: str,
) -> dict[str, Any]:
    """Load one domain's configured instruction or its legacy fallback."""
    repository = validate_wiki_repository(repository)
    if not isinstance(domain, str) or domain not in INSTRUCTION_DOMAINS:
        raise IssueLensConfigError(f"Unsupported instruction domain: {domain}")

    discovered = await _discover_config(client, repository)
    config_path = discovered[0] if discovered else None
    configured_entries = discovered[1] if discovered else {}
    if domain in configured_entries:
        entry = configured_entries[domain]
        instruction_path = entry["path"]
        try:
            content = await _read_text(
                client,
                repository,
                instruction_path,
                maximum_bytes=MAX_INSTRUCTION_BYTES,
            )
        except Exception as error:
            if _is_not_found(error):
                raise IssueLensConfigError(
                    f"Configured instruction file not found: {instruction_path}"
                ) from error
            if isinstance(error, IssueLensConfigError):
                raise
            raise IssueLensConfigError(
                f"Could not load configured instruction {instruction_path}: {error}"
            ) from error
        result: dict[str, Any] = {
            "repository": repository,
            "domain": domain,
            "configStatus": "loaded",
            "configPath": config_path,
            "source": "configured",
            "path": instruction_path,
            "content": content,
        }
        if domain == "team_memory":
            result["wiki_repository"] = entry.get("wiki_repository", repository)
        return result

    for legacy_path in LEGACY_INSTRUCTION_PATHS.get(domain, ()):
        try:
            content = await _read_text(
                client,
                repository,
                legacy_path,
                maximum_bytes=MAX_INSTRUCTION_BYTES,
            )
        except Exception as error:
            if _is_not_found(error):
                continue
            if isinstance(error, IssueLensConfigError):
                raise
            raise IssueLensConfigError(
                f"Could not load legacy instruction {legacy_path}: {error}"
            ) from error
        return {
            "repository": repository,
            "domain": domain,
            "configStatus": "loaded" if discovered else "absent",
            "configPath": config_path,
            "source": "legacy",
            "path": legacy_path,
            "content": content,
        }

    result = {
        "repository": repository,
        "domain": domain,
        "configStatus": "loaded" if discovered else "absent",
        "configPath": config_path,
        "source": "built-in",
        "path": None,
        "content": None,
    }
    if domain == "team_memory":
        result["wiki_repository"] = repository
    return result


async def resolve_wiki_repository(client: Any, repository: str) -> str:
    """Resolve validated team-memory scope; App access is enforced by the caller."""
    instruction = await load_instruction(client, repository, "team_memory")
    return instruction["wiki_repository"]
