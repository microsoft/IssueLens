import base64
import pathlib
import tempfile
import unittest

from azure.ai.agentserver.responses.models import (
    ItemMessage,
    MessageContentInputFileContent,
    MessageContentInputImageContent,
    MessageContentInputTextContent,
)

from media_inputs import (
    MediaInputError,
    invocation_attachments,
    redacted_input_items,
    response_input,
)
from chat import build_input


class MediaInputTests(unittest.TestCase):
    def setUp(self):
        self.image_bytes = b"small png payload"
        self.image_data = base64.b64encode(self.image_bytes).decode("ascii")

    def test_response_image_and_file_become_blob_attachments(self):
        file_data = base64.b64encode(b"issue details").decode("ascii")
        message = ItemMessage(
            role="user",
            content=[
                MessageContentInputTextContent(text="Triage this report"),
                MessageContentInputImageContent(
                    detail="auto",
                    image_url=f"data:image/png;base64,{self.image_data}",
                ),
                MessageContentInputFileContent(
                    filename="details.txt",
                    file_data=file_data,
                ),
            ],
        )

        prompt, attachments = response_input([message])

        self.assertEqual(prompt, "Triage this report")
        self.assertEqual(len(attachments), 2)
        self.assertEqual(attachments[0]["type"], "blob")
        self.assertEqual(attachments[0]["mimeType"], "image/png")
        self.assertEqual(attachments[0]["data"], self.image_data)
        self.assertEqual(attachments[1]["mimeType"], "text/plain")
        self.assertEqual(attachments[1]["displayName"], "details.txt")

    def test_invocation_blob_is_validated_and_preserved(self):
        attachments = invocation_attachments([
            {
                "type": "blob",
                "data": self.image_data,
                "mimeType": "image/png",
                "displayName": "screenshot.png",
            }
        ])

        self.assertEqual(attachments, [
            {
                "type": "blob",
                "data": self.image_data,
                "mimeType": "image/png",
                "displayName": "screenshot.png",
            }
        ])

    def test_invocation_file_path_is_rejected(self):
        with self.assertRaisesRegex(MediaInputError, "inline"):
            invocation_attachments([
                {"type": "file", "path": "C:\\secrets\\key.pem"}
            ])

    def test_remote_response_image_is_rejected(self):
        message = ItemMessage(
            role="user",
            content=[
                MessageContentInputImageContent(
                    detail="auto",
                    image_url="https://example.com/screenshot.png",
                )
            ],
        )

        with self.assertRaisesRegex(MediaInputError, "remote"):
            response_input([message])

    def test_inline_media_is_redacted_from_logs(self):
        message = ItemMessage(
            role="user",
            content=[
                MessageContentInputImageContent(
                    detail="auto",
                    image_url=f"data:image/png;base64,{self.image_data}",
                ),
                MessageContentInputFileContent(
                    filename="details.txt",
                    file_data=self.image_data,
                ),
            ],
        )

        serialized = redacted_input_items([message])
        content = serialized[0]["content"]

        self.assertEqual(content[0]["image_url"], "<redacted data URL>")
        self.assertEqual(content[1]["file_data"], "<redacted>")

    def test_invalid_base64_is_rejected(self):
        with self.assertRaisesRegex(MediaInputError, "invalid base64"):
            invocation_attachments([
                {
                    "type": "blob",
                    "data": "not-base64!",
                    "mimeType": "application/pdf",
                }
            ])

    def test_chat_client_builds_polymorphic_media_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            image = root / "screenshot.png"
            report = root / "report.txt"
            image.write_bytes(self.image_bytes)
            report.write_text("issue details", encoding="utf-8")

            request_input = build_input("Triage this", [image, report])

        content = request_input[0]["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "Triage this"})
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(content[2]["type"], "input_file")
        self.assertEqual(content[2]["filename"], "report.txt")
        self.assertTrue(content[2]["file_data"].startswith("data:text/plain;base64,"))


if __name__ == "__main__":
    unittest.main()
