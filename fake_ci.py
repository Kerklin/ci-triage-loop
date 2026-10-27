"""Fake CI adapter for demos and tests. Replace with your real CI/agent calls."""


class FakeCI:
    def __init__(self, broken=None, human=("refund.test",), fixable=True):
        self.n = 0
        self.broken = dict(broken if broken is not None else {"auth.test": "TokenExpired",
                                      "webhook.test": "Timeout",
                                      "refund.test": "AssertionError"})
        self.human = set(human)
        self.fixable = fixable

    def head(self):
        return (f"c{self.n:06x}", "main")

    def failing_tests(self, commit):
        return list(self.broken.items())

    def classify(self, item, lessons):
        return "human" if item.test_id in self.human else "fix"

    def apply_fixes(self, items, lessons):
        self.n += 1
        if self.fixable:
            for it in items:
                self.broken.pop(it.test_id, None)
        return [f"claude/fix-{it.test_id}" for it in items]

    def verify(self, commit):
        return {"commit": commit, "passed": not self.broken, "skipped": 0,
                "lint_clean": True, "failures": list(self.broken.items())}
