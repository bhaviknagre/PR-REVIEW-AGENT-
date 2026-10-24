"""Offline checks of the scenario library: recorded lines are right, every deterministic
finding is really produced by the static scan, and the clean change produces none."""

import pytest
from scenarios import all_ids, load, load_all

from pr_review.review.static_checks import scan_diff


def _diff(files: dict[str, str]) -> str:
    out = []
    for path, text in files.items():
        lines = text.splitlines()
        out.append(
            f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + ln for ln in lines) + "\n"
        )
    return "".join(out)


def test_library_is_varied() -> None:
    sc = load_all()
    assert len(sc) >= 12
    cats = {f.category for s in sc.values() for f in s.findings}
    assert {"security", "correctness", "performance", "style"} <= cats
    assert any(any(p.endswith(".js") for p in s.files) for s in sc.values())
    assert any(any(p.endswith(".go") for p in s.files) for s in sc.values())
    assert [s.id for s in sc.values() if s.clean] == ["clean_change"]


@pytest.mark.parametrize("sid", all_ids())
def test_static_findings_are_deterministic(sid: str) -> None:
    s = load(sid)
    got = {(f.file_path, f.line_start, f.category) for f in scan_diff(_diff(s.files))}
    want = {(e.path, e.line, e.category) for e in s.findings if e.detector == "static"}
    assert want <= got
    if s.clean:
        assert got == set()
