from pr_review.parsers.chunker import chunk_diff, estimate_tokens
from pr_review.parsers.diff import parse_diff


def git(path: str, body: str, head: str = "") -> str:
    return f"diff --git a/{path} b/{path}\n{head}--- a/{path}\n+++ b/{path}\n{body}"


BODY = "@@ -1,2 +1,3 @@ def f\n a\n-b\n+B\n+c\n"


def test_basic_line_numbers() -> None:
    f = parse_diff(git("x.py", BODY)).files[0]
    assert (f.status, f.path, f.binary) == ("modified", "x.py", False)
    got = [(ln.type, ln.old_no, ln.new_no) for ln in f.hunks[0].lines]
    assert got == [("ctx", 1, 1), ("del", 2, None), ("add", None, 2), ("add", None, 3)]
    assert f.hunks[0].section == "def f"


def test_no_newline_marker_crlf_and_blank_context() -> None:
    d = "--- a/x\r\n+++ b/x\r\n@@ -1,3 +1,3 @@\r\n a\r\n-b\r\n"
    d += "\\ No newline at end of file\r\n+c\r\n\r\n"
    f = parse_diff(d).files[0]
    assert [ln.text for ln in f.hunks[0].lines] == ["a", "b", "c", ""]
    assert f.warnings == ()


def test_statuses_and_dev_null() -> None:
    add = parse_diff("--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n").files[0]
    dele = parse_diff("--- a/o.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n").files[0]
    assert (add.status, add.old_path, add.path) == ("added", None, "n.py")
    assert (dele.status, dele.new_path, dele.path) == ("deleted", None, "o.py")


def test_rename_with_similarity_and_mode_change() -> None:
    d = "diff --git a/a.py b/b.py\nsimilarity index 90%\nrename from a.py\nrename to b.py\n"
    d += "old mode 100644\nnew mode 100755\n"
    f = parse_diff(d).files[0]
    assert (f.status, f.old_path, f.new_path, f.hunks) == ("renamed", "a.py", "b.py", ())


def test_quoted_and_escaped_paths() -> None:
    d = 'diff --git "a/caf\\303\\251 x.py" "b/caf\\303\\251 x.py"\n--- "a/caf\\303\\251 x.py"\t\n'
    d += '+++ "b/caf\\303\\251 x.py"\n@@ -1 +1 @@\n-a\n+b\n'
    assert parse_diff(d).files[0].path == "café x.py"
    sp = parse_diff(git("my dir/f x.py", BODY)).files[0]
    assert sp.path == "my dir/f x.py"


def test_binary() -> None:
    d = "diff --git a/i.dat b/i.dat\nBinary files a/i.dat and b/i.dat differ\n"
    assert parse_diff(d).files[0].binary


def test_malformed_hunk_and_truncated_hunk_warn() -> None:
    p = parse_diff(git("x.py", "@@ -x +y @@\n+1\n"))
    assert "malformed hunk header" in p.files[0].warnings[0]
    p = parse_diff(git("x.py", "@@ -1,5 +1,5 @@\n a\n"))
    assert "shorter" in p.files[0].warnings[0] and len(p.files[0].hunks[0].lines) == 1
    assert parse_diff("@@ -1 +1 @@\n-a\n+b\n").files == ()
    assert parse_diff("\x00garbage\n@@@@\n").files == ()


def test_path_traversal_dropped() -> None:
    d = git("../../etc/passwd", BODY) + git("/abs/p", BODY) + git("ok.py", BODY)
    p = parse_diff(d)
    assert [f.path for f in p.files] == ["ok.py"]
    assert len(p.warnings) == 2


def test_skip_rules_and_reasons() -> None:
    d = "".join(
        git(p, BODY)
        for p in [
            "package-lock.json", "sub/uv.lock", "go.sum", "vendor/x.go", "a/node_modules/b.js",
            "third_party/z.py", "app.min.js", "api_pb2.py", "dist/o.js", "docs/skipme.md",
            "logo.png", "keep.py",
        ]
    )  # fmt: skip
    d += git("gen.py", "@@ -0,0 +1,2 @@\n+# DO NOT EDIT\n+x = 1\n")
    plan = chunk_diff(d, ignore=["docs/*"])
    assert [c.path for c in plan.chunks] == ["keep.py"]
    reasons = {s.path: s.reason for s in plan.skipped}
    assert reasons["go.sum"] == "lock file"
    assert reasons["vendor/x.go"] == "vendored dependency"
    assert reasons["api_pb2.py"] == "generated file (path)"
    assert reasons["gen.py"] == "generated file (marker)"
    assert reasons["logo.png"] == "binary file"
    assert reasons["docs/skipme.md"] == "ignored by config: docs/*"
    assert len(plan.skipped) == 12


def hunk(start: int, n: int) -> str:
    return f"@@ -{start},0 +{start},{n} @@\n" + "".join(f"+line {start + k}\n" for k in range(n))


def test_budget_packing_never_splits_hunk() -> None:
    d = git("x.py", "".join(hunk(s, 5) for s in (1, 20, 40, 60)))
    one = chunk_diff(d, budget=10_000)
    assert len(one.chunks) == 1 and len(one.chunks[0].ranges) == 4
    per = one.chunks[0].tokens // 2
    split = chunk_diff(d, budget=per)
    assert len(split.chunks) >= 2 and not any(c.windowed for c in split.chunks)
    assert all(c.tokens <= per for c in split.chunks)
    assert sum(len(c.ranges) for c in split.chunks) == 4


def test_oversize_hunk_windows_keep_line_numbers() -> None:
    d = git("big.py", hunk(1, 100))
    plan = chunk_diff(d, budget=120, overlap=2)
    assert len(plan.chunks) > 2 and all(c.windowed for c in plan.chunks)
    for c in plan.chunks:
        assert all(t == f"line {n}" for n, t in c.changed_new.items())
    covered = {n for c in plan.chunks for n in c.changed_new}
    assert covered == set(range(1, 101))
    a, b = plan.chunks[0], plan.chunks[1]
    assert set(a.changed_new) & set(b.changed_new)  # overlap
    r = a.ranges[0]
    assert (r.new_start, r.new_len) == (1, len(a.changed_new))


def test_deterministic_ids_and_order() -> None:
    d = git("b.py", BODY) + git("a.py", BODY)
    p1, p2 = chunk_diff(d), chunk_diff(d)
    assert [c.id for c in p1.chunks] == [c.id for c in p2.chunks]
    assert [c.path for c in p1.chunks] == ["a.py", "b.py"]
    assert len({c.id for c in p1.chunks}) == 2
    assert p1.chunks[0].changed_old == {2: "b"}


def test_caps_report_truncation() -> None:
    d = "".join(git(f"f{k}.py", BODY) for k in range(5))
    plan = chunk_diff(d, max_chunks=2)
    assert (len(plan.chunks), plan.truncated, plan.omitted_chunks) == (2, True, 3)
    plan = chunk_diff(d, max_tokens=chunk_diff(d).chunks[0].tokens + 1)
    assert plan.truncated and len(plan.chunks) == 1
    assert not chunk_diff(d).truncated
    assert estimate_tokens("x" * 40) >= 10
