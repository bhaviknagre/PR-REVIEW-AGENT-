from pathlib import Path

import pytest

from pr_review.parsers.chunker import Chunk, HunkRange
from pr_review.parsers.context import attach_context

PY = """import os
import sys

X = 1


@decorator
class Outer:
    def method(self, a):
        return a

    class Inner:
        async def go(self, b: int) -> int:
            y = b + 1
            return y


def top():
    return os.getcwd()
"""


def chunk(path: str, *lines: int, tokens: int = 10) -> Chunk:
    new = {n: "x" for n in lines}
    return Chunk("id", path, (HunkRange(lines[0], len(lines), lines[0], len(lines)),),
                 new, {}, "diff", tokens)  # fmt: skip


def ctx(tmp_path: Path, name: str, src: str, *lines: int, **kw: object) -> tuple[str, ...]:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(src)
    return attach_context([chunk(name, *lines)], tmp_path, **kw)[0].context  # type: ignore[arg-type]


def test_python_nested_async_method(tmp_path: Path) -> None:
    c = ctx(tmp_path, "a.py", PY, 14)
    assert c[0] == "scope: async def Outer.Inner.go (lines 13-15)"
    assert "async def go(self, b: int) -> int:" in c[1]
    assert any("return y" in x for x in c)


def test_python_decorated_class_and_method(tmp_path: Path) -> None:
    assert ctx(tmp_path, "a.py", PY, 10)[0] == "scope: def Outer.method (lines 9-10)"
    assert ctx(tmp_path, "a.py", PY, 8)[0] == "scope: class Outer (lines 7-15)"  # decorator line


def test_python_module_level(tmp_path: Path) -> None:
    c = ctx(tmp_path, "a.py", PY, 4)
    assert c[0] == "scope: module-level"
    assert any(x.startswith("imports: import os; import sys") for x in c)
    assert any("X@4" in x and "top@18" in x for x in c)


def test_python_syntax_error_falls_back_to_window(tmp_path: Path) -> None:
    c = ctx(tmp_path, "a.py", "def f(:\n    pass\n" * 10, 5)
    assert c[0].startswith("warning: python parse failed")
    assert c[1] == "scope: file window"
    assert len(c) > 2


@pytest.mark.parametrize(
    ("name", "src", "line", "expect"),
    [
        ("a.js", "class A {\n  m(x) {\n    return x;\n  }\n}\n", 3, "method_definition m"),
        ("a.js", "const f = (a) => {\n  return a;\n};\n", 2, "arrow_function f"),
        ("a.ts", "export function g(a: number) {\n  return a;\n}\n", 2, "function_declaration g"),
        ("a.tsx", "function C() {\n  return <div/>;\n}\n", 2, "function_declaration C"),
        ("a.go", "package a\n\nfunc (s S) Do() int {\n\treturn 1\n}\n", 4, "method_declaration Do"),
        ("a.rs", "struct S;\nimpl S {\n    fn run(&self) {\n        let a = 1;\n    }\n}\n", 4,
         "function_item run"),
    ],
)  # fmt: skip
def test_treesitter_languages(tmp_path: Path, name: str, src: str, line: int, expect: str) -> None:
    c = ctx(tmp_path, name, src, line)
    assert expect in c[0]
    assert c[1].startswith("signature")


def test_treesitter_module_level_and_error(tmp_path: Path) -> None:
    c = ctx(tmp_path, "m.go", 'package a\n\nimport "fmt"\n\nvar X = 1\n\nfunc F() {}\n', 5)
    assert c[0] == "scope: module-level"
    assert 'imports: import "fmt"' in c
    bad = ctx(tmp_path, "b.ts", "function ( {{{\n" * 5, 2)
    assert bad[0].startswith("warning: typescript parse failed")


def test_budget_trims_context_never_diff(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text(PY)
    c = chunk("a.py", 14, tokens=90)
    full = attach_context([c], tmp_path, budget=1000)[0]
    tight = attach_context([c], tmp_path, budget=100)[0]
    none = attach_context([c], tmp_path, budget=90)[0]
    assert len(tight.context) < len(full.context)
    assert tight.tokens <= 100 and full.tokens > tight.tokens
    assert none.context == () and none.tokens == 90
    assert tight.text == c.text and tight.changed_new == c.changed_new


def test_symlink_ignored(tmp_path: Path) -> None:
    (tmp_path / "real.py").write_text(PY)
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    outside = tmp_path / "out"
    outside.mkdir()
    (outside / "s.py").write_text(PY)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "dir").symlink_to(outside)
    (ws / "l.py").symlink_to(outside / "s.py")
    assert attach_context([chunk("l.py", 14), chunk("dir/s.py", 14)], ws)[0].context == ()
    assert attach_context([chunk("dir/s.py", 14)], ws)[0].context == ()


def test_caps(tmp_path: Path) -> None:
    assert ctx(tmp_path, "a.py", PY, 14, max_file_bytes=10) == ()  # too big and unreadable
    assert ctx(tmp_path, "a.py", PY, 14, max_context_lines=2) == (
        "scope: async def Outer.Inner.go (lines 13-15)",
        ctx(tmp_path, "a.py", PY, 14)[1],
    )
    assert ctx(tmp_path, "a.py", PY, 14, parse_timeout_s=-1)[0].startswith("warning")
    assert ctx(tmp_path, "a.go", "package a\nfunc F() {}\n", 2, parse_timeout_s=-1)[0].startswith(
        "warning"
    )
    assert ctx(tmp_path, "notes.txt", "hello", 1) == ()
    long = "def f():\n    x = '" + "a" * 500 + "'\n"
    assert all(len(x) < 300 for x in ctx(tmp_path, "l.py", long, 2))


def test_context_redacted(tmp_path: Path) -> None:
    src = "def f():\n    t = 'ghp_" + "A1b2C3d4E5" * 3 + "'\n    return t\n"
    c = ctx(tmp_path, "s.py", src, 3)
    assert not any("ghp_" in x for x in c) and any("[REDACTED]" in x for x in c)
