from __future__ import annotations

import json
from pathlib import Path

import pytest

from pr_review.runner import detect as d
from pr_review.runner.detect import Kind, Language, detect


def mk(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_empty_is_unknown(tmp_path: Path) -> None:
    r = detect(tmp_path)
    assert r.kind is Kind.UNKNOWN and r.projects == ()


def test_node_js_vs_ts_and_manager(tmp_path: Path) -> None:
    mk(
        tmp_path,
        {
            "js/package.json": json.dumps({"devDependencies": {"jest": "1"}}),
            "js/yarn.lock": "",
            "ts/package.json": json.dumps({"scripts": {"test": "vitest run"}}),
            "ts/tsconfig.json": "{}",
            "ts/pnpm-lock.yaml": "",
            "dep/package.json": json.dumps({"dependencies": {"typescript": "5"}}),
            "dep/package-lock.json": "",
        },
    )
    by = {p.root: p for p in detect(tmp_path).projects}
    assert (by["js"].typescript, by["js"].package_manager, by["js"].test_framework) == (
        False,
        "yarn",
        "jest",
    )
    assert (by["ts"].typescript, by["ts"].package_manager, by["ts"].test_framework) == (
        True,
        "pnpm",
        "vitest",
    )
    assert by["dep"].typescript and by["dep"].package_manager == "npm"
    assert detect(tmp_path).kind is Kind.SINGLE


def test_python_go_rust(tmp_path: Path) -> None:
    mk(
        tmp_path,
        {
            "py/pyproject.toml": "[tool.pytest.ini_options]\n",
            "py/uv.lock": "",
            "py2/requirements-dev.txt": "requests\n",
            "py2/poetry.lock": "",
            "go/go.mod": "module x\n",
            "rs/Cargo.toml": "[workspace]\nmembers=['a']\n",
            "rs/a/Cargo.toml": "[package]\nname='a'\n",
        },
    )
    by = {p.root: p for p in detect(tmp_path).projects}
    assert (by["py"].package_manager, by["py"].test_framework) == ("uv", "pytest")
    assert (by["py2"].package_manager, by["py2"].test_framework) == ("poetry", "unittest")
    assert by["go"].test_framework == "go test" and by["go"].package_manager == "go"
    assert by["rs"].test_framework == "cargo test" and "rs/a" in by


def test_polyglot_and_root_path(tmp_path: Path) -> None:
    mk(tmp_path, {"package.json": "{}", "svc/go.mod": "module x\n"})
    r = detect(tmp_path)
    assert r.kind is Kind.POLYGLOT
    assert {(p.root, p.language) for p in r.projects} == {
        (".", Language.NODE),
        ("svc", Language.GO),
    }


def test_malformed_manifests_warn(tmp_path: Path) -> None:
    mk(tmp_path, {"a/package.json": "{nope", "b/pyproject.toml": "= bad", "c/Cargo.toml": "[[["})
    r = detect(tmp_path)
    assert len(r.projects) == 3 and all(p.warnings for p in r.projects)


def test_symlink_escape_ignored(tmp_path: Path) -> None:
    outside = mk(tmp_path / "outside", {"go.mod": "module x\n", "package.json": "{}"})
    ws = mk(tmp_path / "ws", {"keep/go.mod": "module y\n"})
    (ws / "link").symlink_to(outside, target_is_directory=True)
    (ws / "keep" / "package.json").symlink_to(outside / "package.json")
    r = detect(ws)
    assert [(p.root, p.language) for p in r.projects] == [("keep", Language.GO)]


def test_vendored_dirs_ignored(tmp_path: Path) -> None:
    mk(tmp_path, {f"{n}/go.mod": "m\n" for n in d.IGNORED_DIRS})
    assert detect(tmp_path).kind is Kind.UNKNOWN


def test_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mk(tmp_path, {"a/b/c/d/e/go.mod": "m\n"})
    assert detect(tmp_path).projects == ()  # deeper than MAX_DEPTH
    mk(tmp_path, {f"p{i}/go.mod": "m\n" for i in range(5)})
    monkeypatch.setattr(d, "MAX_PROJECTS", 2)
    r = detect(tmp_path)
    assert len(r.projects) == 2 and r.truncated
    monkeypatch.setattr(d, "MAX_PROJECTS", 100)
    monkeypatch.setattr(d, "MAX_ENTRIES", 3)
    assert detect(tmp_path).truncated


def test_oversized_manifest_skipped(tmp_path: Path) -> None:
    mk(tmp_path, {"package.json": " " * (d.MAX_MANIFEST_BYTES + 1)})
    (p,) = detect(tmp_path).projects
    assert "exceeds" in p.warnings[0]
