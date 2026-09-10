"""Backward-compatible exports for the shared IssueLens policy loader."""

from github_app_mcp.src.issuelens_github_mcp.policy import (
    CONFIG_DIRECTORY,
    CONFIG_FILENAME,
    CONFIG_VERSION,
    INSTRUCTION_DOMAINS,
    LEGACY_INSTRUCTION_PATHS,
    MAX_CONFIG_BYTES,
    MAX_INSTRUCTION_BYTES,
    IssueLensConfigError,
    load_instruction,
    parse_config,
    resolve_wiki_repository,
    validate_wiki_repository,
)


__all__ = [
    "CONFIG_DIRECTORY",
    "CONFIG_FILENAME",
    "CONFIG_VERSION",
    "INSTRUCTION_DOMAINS",
    "LEGACY_INSTRUCTION_PATHS",
    "MAX_CONFIG_BYTES",
    "MAX_INSTRUCTION_BYTES",
    "IssueLensConfigError",
    "load_instruction",
    "parse_config",
    "resolve_wiki_repository",
    "validate_wiki_repository",
]
