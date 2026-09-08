import tempfile
import shutil
import subprocess
from pathlib import Path
import unittest

import team_memory
from team_memory import ProposalStore, TeamMemoryError, publish_wiki_update
from wiki import WikiRepository


class TeamMemoryTests(unittest.TestCase):
    def test_proposals_are_durable_and_exactly_approvable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProposalStore(str(Path(directory) / "team-memory.db"))
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
        with tempfile.TemporaryDirectory() as directory:
            store = ProposalStore(str(Path(directory) / "team-memory.db"))
            with self.assertRaises(TeamMemoryError):
                store.propose("o/r", "sha", "wiki", {"../x.md": "bad"}, [])
            proposal = store.propose("o/r", "sha", "wiki", {"x.md": "ok"}, [])
            with self.assertRaises(TeamMemoryError):
                store.approve(proposal["proposal_id"], "wrong", "sha", "wiki")

    def test_ephemeral_store_is_rejected(self):
        with self.assertRaises(TeamMemoryError):
            ProposalStore(":memory:")


@unittest.skipUnless(shutil.which("git"), "Git is required for wiki integration tests")
class WikiPublicationTests(unittest.TestCase):
    def test_approved_proposal_updates_local_wiki_and_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare = root / "wiki.git"
            seed = root / "seed"
            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=master", str(bare)],
                check=True, capture_output=True,
            )
            subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
            for key, value in (("user.name", "seed"), ("user.email", "seed@example.com")):
                subprocess.run(["git", "-C", str(seed), "config", key, value], check=True)
            (seed / "Home.md").write_text("human navigation\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "seed"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "master"], check=True, capture_output=True)

            class LocalWiki(WikiRepository):
                def __init__(self, repository, **kwargs):
                    super().__init__(repository, **kwargs)
                    self.remote = str(bare)

            original = team_memory.WikiRepository
            team_memory.WikiRepository = LocalWiki
            try:
                with LocalWiki("o/r", token="test") as wiki:
                    base = wiki.snapshot()["sha"]
                store = ProposalStore(str(root / "proposals.db"))
                proposal = store.propose("o/r", "source-sha", base, {"Architecture.md": "design\n"}, [])
                store.approve(proposal["proposal_id"], proposal["content_hash"], "source-sha", base)
                first = publish_wiki_update(store, proposal["proposal_id"], token="test", author="IssueLens App")
                second = publish_wiki_update(store, proposal["proposal_id"], token="test", author="IssueLens App")
                self.assertEqual(first["status"], "updated")
                self.assertEqual(first["wiki_sha"], second["wiki_sha"])
                with LocalWiki("o/r", token="test") as wiki:
                    self.assertEqual(wiki.page("Home.md")["content"], "human navigation\n")
                    self.assertEqual(wiki.page("Architecture.md")["content"], "design\n")
            finally:
                team_memory.WikiRepository = original

    def test_stale_wiki_base_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare = root / "wiki.git"
            subprocess.run(["git", "init", "--bare", "--initial-branch=master", str(bare)], check=True, capture_output=True)
            seed = root / "seed"
            subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.name", "seed"], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.email", "seed@example.com"], check=True)
            (seed / "Home.md").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "one"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "master"], check=True, capture_output=True)
            class LocalWiki(WikiRepository):
                def __init__(self, repository, **kwargs):
                    super().__init__(repository, **kwargs)
                    self.remote = str(bare)
            original = team_memory.WikiRepository
            team_memory.WikiRepository = LocalWiki
            try:
                with LocalWiki("o/r", token="test") as wiki:
                    base = wiki.snapshot()["sha"]
                store = ProposalStore(str(root / "proposals.db"))
                proposal = store.propose("o/r", "source", base, {"New.md": "new\n"}, [])
                store.approve(proposal["proposal_id"], proposal["content_hash"], "source", base)
                (seed / "Home.md").write_text("human update\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
                subprocess.run(["git", "-C", str(seed), "commit", "-m", "human"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(seed), "push", "origin", "master"], check=True, capture_output=True)
                with self.assertRaises(TeamMemoryError):
                    publish_wiki_update(store, proposal["proposal_id"], token="test", author="IssueLens App")
            finally:
                team_memory.WikiRepository = original


if __name__ == "__main__":
    unittest.main()
