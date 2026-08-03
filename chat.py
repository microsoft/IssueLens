"""Interactive chat client for the local IssueLens agent (/responses protocol).

Usage:
    .\.venv\Scripts\python.exe chat.py            # interactive REPL
    .\.venv\Scripts\python.exe chat.py "prompt"   # one-shot

Keeps the conversation going by chaining previous_response_id, so the agent
resumes the same Copilot session across turns. Type 'exit' to quit, 'new' to
start a fresh conversation.
"""

import json
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8088/responses"


def ask(prompt: str, previous_id: str | None) -> tuple[str, str | None]:
    payload: dict = {"input": prompt, "stream": False}
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
    if len(sys.argv) > 1:
        answer, _ = ask(" ".join(sys.argv[1:]), None)
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
