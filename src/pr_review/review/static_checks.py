"""Deterministic pre-LLM checks (FR-14, NFR-5): secrets, injection sinks, linter findings.

Everything is anchored to added lines, confidence 1.0. `Finding` has no `source` field, so the
origin is a stable summary prefix: `[secret-scan]`, `[pattern]`, `[lint:<tool>]`.
Finding text never contains the matched secret (type + location only).
"""

import posixpath
import re
from collections.abc import Sequence

from pr_review.core.redaction import _PATTERNS, _entropy
from pr_review.core.repo_config import SEVERITY_ORDER, RepoConfig, Severity
from pr_review.parsers.chunker import chunk_diff
from pr_review.parsers.lint import LintResult
from pr_review.review.models import Finding

MAX_LINE = 2000  # bounded scan: longer lines are truncated before any regex runs
_SECRET_TYPES = (
    "private key",
    "GitHub token",
    "GitHub token",
    "AWS access key",
    "bearer token",
    "credentials in URL",
)  # parallel to redaction._PATTERNS
_ASSIGN = re.compile(
    r"""(?i)\b[\w.-]{0,40}(?:password|passwd|secret|api[_-]?key|token|private[_-]?key)[\w.-]{0,20}"""
    r"""["']?\s{0,5}[:=]\s{0,5}["']([^"'\s]{8,200})["']"""
)
_PLACEHOLDER = re.compile(
    r"(?i)example|changeme|change_me|xxxx|placeholder|dummy|your[_-]|\bfake|sample|redacted"
    r"|\btest|<[^>]*>|\$\{|\{\{|\*{4}|environ|getenv|process\.env|os\.env"
)
_ENTROPY = 3.0

_SINKS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    ".py": tuple(
        (n, re.compile(p))
        for n, p in (
            ("eval/exec", r"(?<![\w.])(?:eval|exec)\s*\("),
            ("subprocess with shell=True", r"\bsubprocess\.\w+\s*\([^\n]{0,500}\bshell\s*=\s*True"),
            ("os.system", r"\bos\.(?:system|popen)\s*\("),
            (
                "string-built SQL",
                r"""\.execute\w*\s*\(\s*(?:[fF]["']|["'][^"']{0,500}["']\s*(?:%|\+|\.format\())""",
            ),
            ("pickle.loads", r"\bpickle\.loads?\s*\("),
            ("yaml.load without SafeLoader", r"\byaml\.load\s*\((?![^\n]{0,300}Safe)"),
        )
    ),
    ".js": tuple(
        (n, re.compile(p))
        for n, p in (
            ("eval", r"(?<![\w.])eval\s*\("),
            ("new Function", r"\bnew\s+Function\s*\("),
            (
                "child_process.exec with concatenation",
                r"(?:child_process\.|(?<![\w.]))exec(?:Sync)?\s*\([^)\n]{0,300}(?:\+|\$\{)",
            ),
            ("innerHTML assignment", r"\.innerHTML\s*\+?="),
            ("dangerouslySetInnerHTML", r"\bdangerouslySetInnerHTML\b"),
        )
    ),
    ".go": tuple(
        (n, re.compile(p))
        for n, p in (
            (
                "exec.Command shell",
                r"""\bexec\.Command(?:Context)?\s*\([^"\n]{0,100}"(?:sh|bash)"\s*,\s*"-c\"""",
            ),
            (
                "string-built SQL",
                r"""\.(?:Query|QueryRow|Exec)(?:Context)?\s*\([^\n]{0,200}?(?:fmt\.Sprintf\s*\(|"\s*\+)""",
            ),
        )
    ),
    ".rs": tuple(
        (n, re.compile(p))
        for n, p in (
            (
                "Command::new shell with format!",
                r"""Command::new\s*\(\s*"(?:sh|bash)"\s*\)[^\n]{0,300}format!""",
            ),
        )
    ),
}
for _ext in (".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
    _SINKS[_ext] = _SINKS[".js"]

_SINK_RATIONALE = (
    "This construct can turn untrusted input into code, a shell command or a query. "
    "Prefer a safe API (parameterised queries, argument lists, safe loaders, textContent)."
)
_SECRET_RATIONALE = (
    "A credential appears to be committed in source. Rotate it, remove it from history and load "
    "it from the environment or a secret manager instead."
)
# Linter severities -> Finding severities. Ruff reports only "warning", ESLint "error" at level 2.
_LINT_SEVERITY: dict[str, Severity] = {"error": "MAJOR", "warning": "MINOR", "info": "INFO"}


def _secret_type(line: str) -> str | None:
    for pat, kind in zip(_PATTERNS, _SECRET_TYPES, strict=True):
        if (m := pat.search(line)) and not _PLACEHOLDER.search(m.group()):
            return kind
    for m in _ASSIGN.finditer(line):
        v = m[1]
        if (
            not _PLACEHOLDER.search(v)
            and not _PLACEHOLDER.search(line[: m.start(1)].rsplit("=", 1)[-1])
            and not v.isalpha()
            and _entropy(v) >= _ENTROPY
        ):
            return "high-entropy credential assignment"
    return None


def _finding(
    severity: Severity, category: str, path: str, line: int, summary: str, rationale: str
) -> Finding:
    return Finding(
        severity=severity,
        category=category,  # type: ignore[arg-type]
        file_path=path,
        line_start=line,
        line_end=line,
        summary=summary[:300],
        rationale=rationale,
        confidence=1.0,
    )


def scan_diff(diff_text: str, config: RepoConfig | None = None) -> list[Finding]:
    """Secret and injection-sink findings on added lines only."""
    cfg = config or RepoConfig()
    out: dict[tuple[str, int, str], Finding] = {}
    for chunk in chunk_diff(diff_text, ignore=cfg.ignore).chunks:
        sinks = _SINKS.get(posixpath.splitext(chunk.path)[1].lower(), ())
        for no, text in chunk.changed_new.items():
            text = text[:MAX_LINE]
            if kind := _secret_type(text):
                out[(chunk.path, no, "secret")] = _finding(
                    "CRITICAL",
                    "security",
                    chunk.path,
                    no,
                    f"[secret-scan] Possible hard-coded {kind} at {chunk.path}:{no}",
                    _SECRET_RATIONALE,
                )
            for name, pat in sinks:
                if pat.search(text):
                    out[(chunk.path, no, name)] = _finding(
                        "MAJOR",
                        "security",
                        chunk.path,
                        no,
                        f"[pattern] Injection sink ({name}) at {chunk.path}:{no}",
                        _SINK_RATIONALE,
                    )
    return list(out.values())


def merge_lint(
    diff_text: str, results: Sequence[LintResult], config: RepoConfig | None = None
) -> list[Finding]:
    """Linter issues on added lines as style findings, deduped by (file, line, rule)."""
    cfg = config or RepoConfig()
    changed: dict[str, dict[int, str]] = {}
    for chunk in chunk_diff(diff_text, ignore=cfg.ignore).chunks:
        changed.setdefault(chunk.path, {}).update(chunk.changed_new)
    out: dict[tuple[str, int, str], Finding] = {}
    for res in results:
        for i in res.issues:
            if i.line in changed.get(i.file, {}) and (i.file, i.line, i.rule) not in out:
                out[(i.file, i.line, i.rule)] = _finding(
                    _LINT_SEVERITY[i.severity],
                    "style",
                    i.file,
                    i.line,
                    f"[lint:{res.tool}] {i.rule}: {i.message}",
                    f"{res.tool} reported rule {i.rule} on a line changed in this diff.",
                )
    return list(out.values())


def static_findings(
    diff_text: str, lint: Sequence[LintResult] = (), config: RepoConfig | None = None
) -> list[Finding]:
    """All deterministic findings, filtered by repo-config categories and min severity."""
    cfg = config or RepoConfig()
    floor = SEVERITY_ORDER.index(cfg.min_severity)
    return [
        f
        for f in scan_diff(diff_text, cfg) + merge_lint(diff_text, lint, cfg)
        if f.category in cfg.categories and SEVERITY_ORDER.index(f.severity) <= floor
    ]
