"""
ci-triage loop as an explicit state machine.
State is persisted after every transition, so a crash or restart resumes where it left off.
"""
from __future__ import annotations
import hashlib, json, time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path

class S(str, Enum):
    IDLE = "IDLE"; DISCOVER = "DISCOVER"; PLAN = "PLAN"; EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"; DECIDE = "DECIDE"
    DONE = "DONE"; ESCALATED = "ESCALATED"          # terminal states

TERMINAL = {S.DONE, S.ESCALATED}
MAX_ROUNDS, MAX_STEP_RETRIES, STALE_HOURS = 5, 2, 48

TRANSITIONS = {
    S.IDLE:     {S.DISCOVER},
    S.DISCOVER: {S.PLAN, S.VERIFY},
    S.PLAN:     {S.EXECUTE, S.ESCALATED},
    S.EXECUTE:  {S.VERIFY},
    S.VERIFY:   {S.DECIDE},
    S.DECIDE:   {S.DONE, S.DISCOVER, S.ESCALATED},
}

class VerifyMismatch(RuntimeError):
    """VERIFY ran on a different commit than the one under test."""


def fingerprint(test_id: str, error: str) -> str:
    """Same test + same first error line = same failure. Used to detect 'no progress'."""
    first = (error or "").strip().splitlines()[0] if (error or "").strip() else ""
    return hashlib.sha1(f"{test_id}|{first}".encode()).hexdigest()[:10]

@dataclass
class Item:
    fp: str; test_id: str; error: str
    status: str = "open"
    branch: str | None = None
    since: float = field(default_factory=time.time)

@dataclass
class LoopState:
    goal: str
    state: S = S.IDLE
    round: int = 0
    commit: str | None = None
    branch: str | None = None
    items: dict[str, Item] = field(default_factory=dict)
    seen: list[list[str]] = field(default_factory=list)
    last_verify: dict | None = None
    lessons: list[str] = field(default_factory=list)
    retries: int = 0
    reason: str | None = None
    log: list[dict] = field(default_factory=list)

    def save(self, path: Path):
        d = asdict(self); d["state"] = self.state.value
        path.write_text(json.dumps(d, indent=2))

    @classmethod
    def load(cls, path: Path, goal: str) -> "LoopState":
        if not path.exists(): return cls(goal=goal)
        d = json.loads(path.read_text())
        d["state"] = S(d["state"])
        d["items"] = {k: Item(**v) for k, v in d["items"].items()}
        return cls(**d)

class Machine:
    def __init__(self, ctx, path: Path, goal: str):
        self.ctx, self.path = ctx, path
        self.st = LoopState.load(path, goal)

    def go(self, nxt: S, note: str = ""):
        if nxt not in TRANSITIONS[self.st.state]:
            raise RuntimeError(f"illegal transition {self.st.state} -> {nxt}")
        self.st.log.append({"t": time.time(), "from": self.st.state.value,
                            "to": nxt.value, "round": self.st.round,
                            "commit": self.st.commit, "note": note})
        self.st.state, self.st.retries = nxt, 0
        self.st.save(self.path)

    def run(self) -> LoopState:
        while self.st.state not in TERMINAL:
            handler = getattr(self, f"on_{self.st.state.value.lower()}")
            try:
                handler()
            except Exception as e:
                self.st.retries += 1
                self.st.save(self.path)
                if self.st.retries > MAX_STEP_RETRIES:
                    self.st.reason = f"{self.st.state.value} failed: {e}"
                    self.st.log.append({"t": time.time(), "from": self.st.state.value,
                                        "to": "ESCALATED", "note": self.st.reason})
                    self.st.state = S.ESCALATED
                    self.st.save(self.path)
        return self.st

    @staticmethod
    def _stale(it: Item) -> bool:
        return time.time() - it.since > STALE_HOURS * 3600

    def on_idle(self):
        self.st.round = 1
        self.go(S.DISCOVER, "triggered")

    def on_discover(self):
        self.st.commit, self.st.branch = self.ctx.head()
        failures = self.ctx.failing_tests(self.st.commit)
        for test_id, err in failures:
            fp = fingerprint(test_id, err)
            it = self.st.items.setdefault(fp, Item(fp, test_id, err))
            # still failing after a fix attempt, or a fixed test that regressed: reopen
            if it.status in ("fixing", "fixed") and not self._stale(it):
                it.status, it.since = "open", time.time()
        for it in self.st.items.values():
            if it.status == "fixing" and self._stale(it):
                it.status = "escalated"
        open_items = [i for i in self.st.items.values() if i.status == "open"]
        self.go(S.PLAN if open_items else S.VERIFY, f"{len(failures)} failing")

    def on_plan(self):
        for it in self.st.items.values():
            if it.status != "open": continue
            decision = self.ctx.classify(it, self.st.lessons)
            it.status = "fixing" if decision == "fix" else "escalated"
            it.since = time.time()
        if any(i.status == "fixing" for i in self.st.items.values()):
            self.go(S.EXECUTE)
        else:
            self.st.reason = "nothing fixable by the agent"
            self.go(S.ESCALATED, self.st.reason)

    def on_execute(self):
        fixing = [i for i in self.st.items.values() if i.status == "fixing"]
        for it, branch in zip(fixing, self.ctx.apply_fixes(fixing, self.st.lessons)):
            it.branch = branch
        self.st.commit, self.st.branch = self.ctx.head()
        self.go(S.VERIFY, f"{len(fixing)} fixes applied")

    def on_verify(self):
        r = self.ctx.verify(self.st.commit)
        if r.get("commit") != self.st.commit:   # explicit check: survives python -O
            raise VerifyMismatch(f"verified {r.get('commit')}, expected {self.st.commit}")
        self.st.last_verify = r
        self.go(S.DECIDE, f"passed={r['passed']} skipped={r['skipped']}")

    def on_decide(self):
        r = self.st.last_verify
        fps = sorted(fingerprint(t, e) for t, e in r["failures"])
        for it in self.st.items.values():
            if it.status == "fixing" and it.fp not in fps: it.status = "fixed"

        if r["passed"] and r["skipped"] == 0 and r["lint_clean"]:
            self.st.reason = f"goal met on {self.st.commit}"
            return self.go(S.DONE, self.st.reason)

        if r["skipped"] and not r["failures"]:
            self.st.reason = f"{r['skipped']} tests skipped; goal not provable"
            return self.go(S.ESCALATED, self.st.reason)

        # no progress = nothing from last round's failures got fixed
        if self.st.seen and set(fps) and set(fps) >= set(self.st.seen[-1]):
            self.st.reason = "no progress: same failures as last round"
            return self.go(S.ESCALATED, self.st.reason)

        if self.st.round >= MAX_ROUNDS:
            self.st.reason = f"round limit {MAX_ROUNDS} reached"
            return self.go(S.ESCALATED, self.st.reason)

        remaining = [i for i in self.st.items.values() if i.fp in fps]
        if remaining and all(i.status == "escalated" for i in remaining):
            self.st.reason = "only human-owned failures remain"
            return self.go(S.ESCALATED, self.st.reason)

        self.st.seen.append(fps)
        self.st.round += 1
        self.go(S.DISCOVER, "iterate")
