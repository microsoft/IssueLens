"""Build a Functions source ZIP without copying host code, config, or secrets."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build(output: Path) -> None:
    source = Path(__file__).resolve().parent
    root = source.parent
    action = root / ".github" / "actions" / "issuelens"
    files = (
        source / "function_app.py", source / "bridge.py",
        source / "host.json", source / "requirements.txt",
        action / "issuelens_action.py", action / "stream_output.py",
    )
    with ZipFile(output, "x", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New ZIP file to create")
    build(parser.parse_args().output)
