import tempfile
import unittest

from team_memory import ProposalStore, TeamMemoryError


class TeamMemoryTests(unittest.TestCase):
    def test_proposals_are_durable_and_exactly_approvable(self):
        with tempfile.NamedTemporaryFile() as database:
            store = ProposalStore(database.name)
            proposal = store.propose(
                "microsoft/IssueLens", "abc1234", "wiki-sha",
                {"architecture.md": "Evidence-backed content"},
                [{"source": "https://github.com/microsoft/IssueLens/pull/1"}],
            )
            self.assertEqual(store.get(proposal["proposal_id"])["status"], "proposed")
            store.approve(
                proposal["proposal_id"], proposal["content_hash"],
                proposal["source_revision"], proposal["wiki_base"],
            )
            self.assertEqual(store.get(proposal["proposal_id"])["status"], "approved")

    def test_stale_approval_and_unsafe_pages_fail_closed(self):
        with tempfile.NamedTemporaryFile() as database:
            store = ProposalStore(database.name)
            with self.assertRaises(TeamMemoryError):
                store.propose("o/r", "sha", "wiki", {"../x.md": "bad"}, [])
            proposal = store.propose("o/r", "sha", "wiki", {"x.md": "ok"}, [])
            with self.assertRaises(TeamMemoryError):
                store.approve(proposal["proposal_id"], "wrong", "sha", "wiki")

    def test_ephemeral_store_is_rejected(self):
        with self.assertRaises(TeamMemoryError):
            ProposalStore(":memory:")


if __name__ == "__main__":
    unittest.main()
