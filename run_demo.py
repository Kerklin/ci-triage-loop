"""Run: python examples/run_demo.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ci_triage import Machine  # noqa: E402
from fake_ci import FakeCI     # noqa: E402

p = Path("state.json")
p.unlink(missing_ok=True)
st = Machine(FakeCI(), p, "all tests pass + lint clean").run()
for e in st.log:
    print(f"r{e.get('round', '-')} {e['from']:9}-> {e['to']:9} {e['note']}")
print("FINAL:", st.state.value, "|", st.reason)
print({i.test_id: i.status for i in st.items.values()})
