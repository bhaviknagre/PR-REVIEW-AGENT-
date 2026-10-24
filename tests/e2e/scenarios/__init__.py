"""Varied scenario library for the real tests.

Each directory holds `scenario.json` (title, expected findings, failing-test count) and `files/`
with the repository files the pull request adds. Files carry a `.fixture` suffix on disk so ruff,
pytest and editors never treat the deliberately buggy code as part of this repository; the suffix
is stripped to give the path in the sandbox repo. `detector` says who is expected to report a
finding: "static" (deterministic scan, must always appear), "lint" (linter, when it runs) or
"llm" (model dependent, asserted with a tolerance).
"""

import json
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Expected:
    category: str
    path: str
    line: int
    anchor: str
    detector: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    clean: bool
    failing_tests: int
    files: dict[str, str]
    findings: tuple[Expected, ...]

    @property
    def has_python(self) -> bool:
        return any(p.endswith(".py") for p in self.files)


def load(scenario_id: str) -> Scenario:
    d = HERE / scenario_id
    meta = json.loads((d / "scenario.json").read_text())
    files = {
        str(p.relative_to(d / "files")).removesuffix(".fixture"): p.read_text()
        for p in sorted((d / "files").rglob("*.fixture"))
    }
    findings = tuple(Expected(**{**f, "keywords": tuple(f["keywords"])}) for f in meta["findings"])
    for f in findings:  # the recorded line must really hold the anchor text
        assert f.anchor in files[f.path].splitlines()[f.line - 1], (scenario_id, f)
    return Scenario(
        meta["id"], meta["title"], meta["clean"], meta["failing_tests"], files, findings
    )


def all_ids() -> list[str]:
    return sorted(p.parent.name for p in HERE.glob("*/scenario.json"))


def load_all() -> dict[str, Scenario]:
    return {i: load(i) for i in all_ids()}


def repo_files(*ids: str, requirements: bool = True) -> dict[str, str]:
    """The combined file set of several scenarios as one pull request's content."""
    out: dict[str, str] = {}
    for i in ids:
        s = load(i)
        clash = out.keys() & s.files.keys()
        assert not clash, f"scenario files collide: {clash}"
        out.update(s.files)
    if requirements and any(p.endswith(".py") for p in out):
        yaml = "pyyaml\n" if any("import yaml" in c for c in out.values()) else ""
        out["requirements.txt"] = "pytest\ncoverage\n" + yaml
    return out


def expected(*ids: str) -> list[Expected]:
    return [f for i in ids for f in load(i).findings]
