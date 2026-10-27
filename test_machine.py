import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "examples")]

from ci_triage import Machine, S, fingerprint  # noqa: E402
from fake_ci import FakeCI                     # noqa: E402

GOAL = "all tests pass + lint clean"


class OnePerRound(FakeCI):
    """Fixes only one failure per round: slow but real progress."""
    def apply_fixes(self, items, lessons):
        self.n += 1
        if self.broken:
            self.broken.pop(sorted(self.broken)[0])
        return [f"claude/fix-{it.test_id}" for it in items]


class FixOnSecondTry(FakeCI):
    """First fix attempt fails, second succeeds."""
    def apply_fixes(self, items, lessons):
        self.n += 1
        if self.n >= 2:
            self.broken.clear()
        return [f"claude/fix-{it.test_id}" for it in items]


class MachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, ci):
        return Machine(ci, self.path, GOAL).run()

    def test_all_fixable_reaches_done(self):
        st = self.run_with(FakeCI(human=()))
        self.assertEqual(st.state, S.DONE)
        self.assertIn("goal met", st.reason)

    def test_human_only_failure_escalates(self):
        st = self.run_with(FakeCI())
        self.assertEqual(st.state, S.ESCALATED)
        self.assertEqual(st.reason, "only human-owned failures remain")

    def test_no_progress_escalates(self):
        st = self.run_with(FakeCI(human=(), fixable=False))
        self.assertEqual(st.state, S.ESCALATED)
        self.assertIn("no progress", st.reason)

    def test_partial_progress_keeps_iterating(self):
        st = self.run_with(OnePerRound(broken={"a.t": "E", "b.t": "E", "c.t": "E"}, human=()))
        self.assertEqual(st.state, S.DONE)
        self.assertEqual(st.round, 3)

    def test_failed_fix_is_retried(self):
        st = self.run_with(FixOnSecondTry(broken={"a.t": "E"}, human=()))
        self.assertEqual(st.state, S.DONE)
        self.assertEqual(st.round, 2)

    def test_empty_suite_with_skips_blocks_done(self):
        ci = FakeCI(broken={}, human=())
        self.assertEqual(ci.broken, {})
        ci.verify = lambda c: {"commit": c, "passed": True, "skipped": 2,
                               "lint_clean": True, "failures": []}
        st = self.run_with(ci)
        self.assertEqual(st.state, S.ESCALATED)
        self.assertIn("skipped", st.reason)

    def test_wrong_commit_verified_escalates(self):
        ci = FakeCI(human=())
        ci.verify = lambda c: {"commit": "other", "passed": True, "skipped": 0,
                               "lint_clean": True, "failures": []}
        st = self.run_with(ci)
        self.assertEqual(st.state, S.ESCALATED)
        self.assertIn("VERIFY failed", st.reason)

    def test_fingerprint_handles_empty_error(self):
        self.assertEqual(len(fingerprint("t", "")), 10)
        self.assertEqual(len(fingerprint("t", None)), 10)

    def test_state_persisted_and_resumable(self):
        self.run_with(FakeCI(human=()))
        self.assertTrue(self.path.exists())
        resumed = Machine(FakeCI(human=()), self.path, GOAL)
        self.assertEqual(resumed.st.state, S.DONE)

    def test_illegal_transition_raises(self):
        m = Machine(FakeCI(), self.path, GOAL)
        with self.assertRaises(RuntimeError):
            m.go(S.DONE)


if __name__ == "__main__":
    unittest.main()
