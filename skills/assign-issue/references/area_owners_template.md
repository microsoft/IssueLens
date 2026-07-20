# Area Owners Template

This file defines technical areas and their owners for automatic issue routing.
Copy it to `.github/area_owners.md`, `docs/area_owners.md`, or
`area_owners.md` in the target repository and replace the example owners.

## Format

Each area contains:

- **Keywords**: terms that indicate an issue belongs to the area.
- **Paths**: optional file or directory glob patterns associated with the area.
- **Owners**: individual GitHub usernames or team handles responsible for the
  area.

## Debugging

- **Keywords**: debugger, breakpoint, launch.json, debug console, debug adapter, DAP, attach, remote debug
- **Paths**: src/debugger/**, extension/debug/**, launch.json
- **Owners**: @debugger-owner

## Language Server / IntelliSense

- **Keywords**: IntelliSense, completion, hover, go to definition, LSP, language server, code actions, diagnostics, symbols
- **Paths**: src/languageserver/**, src/lsp/**
- **Owners**: @lsp-owner

## Build and Compile

- **Keywords**: build, compile, maven, gradle, ant, classpath, build path, compilation error, project import
- **Paths**: src/build/**, src/project/**
- **Owners**: @build-owner

## Testing

- **Keywords**: test, junit, testng, test runner, test explorer, test discovery, coverage
- **Paths**: src/test/**, src/testing/**
- **Owners**: @test-owner

## Documentation

- **Keywords**: docs, documentation, readme, wiki, help, tutorial
- **Paths**: docs/**, *.md
- **Owners**: @docs-owner

## Notes

1. Match keywords case-insensitively against the issue title, body, and labels.
2. Match paths against file paths mentioned in the issue.
3. Individual usernames can be assigned directly. Team handles are routing
   hints and must be resolved to an eligible individual before assignment.
4. When multiple areas match, prefer the area with the strongest keyword and
   path evidence.