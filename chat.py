r"""Interactive chat client for the local IssueLens agent (/responses protocol).

Usage:
    .\.venv\Scripts\python.exe chat.py            # interactive REPL
    .\.venv\Scripts\python.exe chat.py "prompt"   # one-shot
    .\.venv\Scripts\python.exe chat.py --attach screenshot.png "prompt"

Keeps the conversation going by chaining previous_response_id, so the agent
resumes the same Copilot session across turns. Type 'exit' to quit, 'new' to
start a fresh conversation.
"""

import argparse
import base64
import json
import mimetypes
import pathlib
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8088/responses"


def build_input(prompt: str, attachment_paths: list[pathlib.Path]) -> str | list[dict]:
    if not attachment_paths:
        return prompt

    content: list[dict] = []
    if prompt:
        content.append({"type": "input_text", "text": prompt})
    for path in attachment_paths:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        data_url = f"data:{mime_type};base64,{encoded}"
        if mime_type.startswith("image/"):
            content.append({
                "type": "input_image",
                "image_url": data_url,
                "detail": "auto",
            })
        else:
            content.append({
                "type": "input_file",
                "filename": path.name,
                "file_data": data_url,
            })
    return [{"type": "message", "role": "user", "content": content}]


def ask(
    prompt: str,
    previous_id: str | None,
    attachment_paths: list[pathlib.Path] | None = None,
) -> tuple[str, str | None]:
    payload: dict = {
        "input": build_input(prompt, attachment_paths or []),
        "stream": False,
    }
    if previous_id:
        payload["previous_response_id"] = previous_id
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.loads(response.read())
    except urllib.error.URLError as exc:
        return f"[request failed: {exc}]", previous_id

    parts = [
        content["text"]
        for item in body.get("output", [])
        for content in item.get("content", [])
        if content.get("text")
    ]
    return "\n".join(parts) or "[no text in response]", body.get("id")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with a local IssueLens agent")
    parser.add_argument("prompt", nargs="*", help="one-shot prompt")
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        type=pathlib.Path,
        metavar="PATH",
        help="attach an image or file; repeat for multiple files",
    )
    args = parser.parse_args()

    if args.prompt or args.attach:
        answer, _ = ask(" ".join(args.prompt), None, args.attach)
        print(answer)
        return

    print("IssueLens local chat — 'exit' to quit, 'new' to reset the conversation.\n")
    previous_id: str | None = None
    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            return
        if prompt.lower() == "new":
            previous_id = None
            print("(started a new conversation)\n")
            continue
        answer, previous_id = ask(prompt, previous_id)
        print(f"\nissuelens> {answer}\n")


if __name__ == "__main__":
    main()
