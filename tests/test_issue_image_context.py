import unittest

from issue_image_context import issue_image_attachments, issue_references


class IssueImageContextTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_urls_and_short_references_once_in_order(self):
        prompt = (
            "Triage https://github.com/microsoft/IssueLens/issues/7, then "
            "microsoft/other#3 and microsoft/issuelens#7."
        )

        self.assertEqual(issue_references(prompt), [
            ("microsoft/IssueLens", 7),
            ("microsoft/other", 3),
        ])

    async def test_issue_url_images_become_copilot_blob_attachments(self):
        class Client:
            async def get_issue_images(self, repository, issue_number):
                self.call = (repository, issue_number)
                return {
                    "images": [{
                        "data": "aW1hZ2U=",
                        "mime_type": "image/png",
                    }],
                }

        client = Client()
        attachments = await issue_image_attachments(
            "triage this issue: "
            "https://github.com/microsoft/IssueLens/issues/7",
            client,
        )

        self.assertEqual(client.call, ("microsoft/IssueLens", 7))
        self.assertEqual(attachments, [{
            "type": "blob",
            "data": "aW1hZ2U=",
            "mimeType": "image/png",
            "displayName": "microsoft-IssueLens-7-image-1.png",
        }])


if __name__ == "__main__":
    unittest.main()
