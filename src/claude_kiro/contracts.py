"""`ck lint --contracts`: string-literal keys in test files that design.md never names.

Test tasks written in parallel drift from the design's data models (bench v2: two
test files disagreed with the design about `command` / `intent_id` / `state`),
and a reviewer reading one diff at a time misses it. This is a cheap first pass.
It catches invented and misspelt names, not a real name used on the wrong model
(the bench case: every one of those names is somewhere in that design); the
reviewer's contract check and the cross-test consistency step cover that.

- Known names come from design.md's code blocks (every identifier and quoted
  word), its tables (every word in a `|` row: Data Models and API tables) and
  backticked spans anywhere. Prose doesn't count, so a word used only in a
  sentence is not a defined field.
- Test files are scanned for keys used against data: `x["key"]`, `.get("key")`,
  `{"key": ...}` / `, "key": ...` in dict literals, and status-like values
  compared with `==` / `!=` (`== "pending"`). Only lower_snake_case words are
  checked, which keeps headers, env vars, messages and camelCase JS out; so are
  test-name maps (`test_*` keys) and dunders (`__main__`).

It is a heuristic, so it warns; `--strict` makes it fail. Keys a project uses
on purpose (fixtures, framework fields) go in specs/ck.json "contracts_ignore".
"""

import difflib
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from claude_kiro.config import load_config
from claude_kiro.hooks._shared.spec_parser import parse_tasks
from claude_kiro.hooks._shared.test_heuristics import is_test_path

WORD = r"[a-z_][a-z0-9_]*"
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FENCE = re.compile(r"^\s*(```|~~~)")
BACKTICK = re.compile(r"`([^`\n]+)`")
TEST_PATTERNS = [
    ("key", re.compile(rf"\[\s*['\"]({WORD})['\"]\s*\]")),
    ("key", re.compile(rf"\.get\(\s*['\"]({WORD})['\"]")),
    ("key", re.compile(rf"[{{,]\s*['\"]({WORD})['\"]\s*:")),
    ("value", re.compile(rf"[=!]=\s*['\"]({WORD})['\"]")),
    ("value", re.compile(rf"['\"]({WORD})['\"]\s*[=!]=")),
]


def design_names(text: str) -> Set[str]:
    """Names design.md defines: identifiers in code blocks, table rows, and backticks."""
    names: Set[str] = set()
    in_code = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
            continue
        if in_code or line.lstrip().startswith("|"):
            names.update(IDENT.findall(line))
        for span in BACKTICK.findall(line):
            names.update(IDENT.findall(span))
    return names


def used_keys(text: str) -> List[Dict]:
    """[{line, key, kind}] for each key/value use, first occurrence per (key, kind) and line."""
    out, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//")):
            continue
        for kind, pattern in TEST_PATTERNS:
            for key in pattern.findall(line):
                if (number, key, kind) not in seen:
                    seen.add((number, key, kind))
                    out.append({"line": number, "key": key, "kind": kind})
    return out


def _git_changed(project_dir: Path, base: str) -> List[str]:
    def run(*args: str) -> List[str]:
        proc = subprocess.run(["git", "-C", str(project_dir), *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise ValueError(proc.stderr.strip() or f"git {' '.join(args)} failed")
        return [line for line in proc.stdout.splitlines() if line]

    # Committed and uncommitted changes against base, plus new untracked files.
    return run("diff", "--name-only", "--diff-filter=AMR", base) + run("ls-files", "--others", "--exclude-standard")


def _spec_test_files(spec_dir: Path, project_dir: Path, config: dict, only: Optional[Set[str]]) -> List[str]:
    from claude_kiro.lint import _test_files

    files: List[str] = []
    for task in parse_tasks(spec_dir / "tasks.md"):
        if task.track == "test" and (not only or task.num in only):
            files += _test_files(task, project_dir, config)
    return files


def check_contracts(
    spec_dir: Path,
    project_dir: Path,
    tasks: Iterable[str] = (),
    base: Optional[str] = None,
    files: Iterable[str] = (),
) -> Dict:
    """Unknown keys in the spec's test files (or the ones changed since base, or `files`)."""
    config = load_config(project_dir)
    design = spec_dir / "design.md"
    known = design_names(design.read_text(errors="replace")) if design.is_file() else set()
    only = {str(t) for t in tasks}
    if files:
        candidates = list(files)
    elif base:
        candidates = [f for f in _git_changed(project_dir, base) if is_test_path(f, config)]
    else:
        candidates = _spec_test_files(spec_dir, project_dir, config, only)
    checked = sorted({f.removeprefix("./") for f in candidates if (project_dir / f).is_file()})
    result: Dict = {"design": str(design), "designNames": len(known), "checkedFiles": checked, "unknown": [], "skipped": None}
    if not known:
        result["skipped"] = f"{design.name} has no code blocks, tables or backticked names to check against"
        return result
    ignore = {str(k) for k in (config.get("contracts_ignore") or []) if isinstance(k, str)}
    known_list = sorted(n for n in known if n == n.lower())
    for rel in checked:
        try:
            text = (project_dir / rel).read_text(errors="replace")
        except OSError:
            continue
        reported: Set[str] = set()
        for use in used_keys(text):
            key = use["key"]
            if key in known or key in ignore or key in reported or key.startswith(("test_", "__")):
                continue
            reported.add(key)
            close = difflib.get_close_matches(key, known_list, n=1, cutoff=0.8)
            result["unknown"].append({"file": rel, **use, "suggest": close[0] if close else None})
    return result
