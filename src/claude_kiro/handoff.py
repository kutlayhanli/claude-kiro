"""Pure decisions for running a spec skill from a background-session worktree.

`decide_handoff` (design S11) says whether `ck handoff` must launch `ck run`
from the main checkout instead of letting the skill run in an isolated
`.claude/worktrees/` session. `is_headless` (S5) says whether the current
process is an unattended run. No I/O, no clock, no environment reads.
"""

import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Mapping, Optional, Tuple

HANDOFF_KINDS = ("implement", "create", "review")
_TASK_TOKEN = re.compile(r"^[0-9][0-9A-Za-z_]*$")


def is_headless(env: Mapping[str, str]) -> bool:
    """True for every `ck run` child (CK_RUN_HEADLESS=1) and for `claude -p` (sdk entrypoint)."""
    try:
        if env.get("CK_RUN_HEADLESS") == "1":
            return True
        entry = env.get("CLAUDE_CODE_ENTRYPOINT")
    except AttributeError:
        return False
    return isinstance(entry, str) and entry.startswith("sdk")


@dataclass(frozen=True)
class HandoffFacts:
    kind: str                                  # implement | create | review | other
    args: Tuple[str, ...] = ()                 # the skill's $ARGUMENTS, split shell-style
    cwd: str = ""                              # absolute, resolved
    is_linked_worktree: bool = False           # git-dir != git-common-dir
    main_root: Optional[str] = None            # the main checkout's top level
    spec_in_main: bool = False                 # specs/<spec> exists at the main HEAD
    spec_in_worktree: bool = False             # specs/<spec> exists at the worktree HEAD
    worktree_head: Optional[str] = None        # HEAD sha of the worktree
    main_head: Optional[str] = None            # HEAD sha of the main checkout
    main_branch: Optional[str] = None          # branch checked out in the main checkout
    main_clean: Optional[bool] = None          # main checkout has no uncommitted changes
    spec_same: Optional[bool] = None           # same specs/<spec> tree id at both HEADs
    worktree_spec_dirty: Optional[bool] = None  # uncommitted changes under specs/<spec> in the worktree
    run_branch_at: Optional[str] = None        # commit of an existing `<spec>-run` branch, if any


@dataclass(frozen=True)
class HandoffDecision:
    action: str                                # "none" | "handoff" | "refuse"
    reason: str = ""
    main_checkout: Optional[str] = None
    run_args: Tuple[str, ...] = ()             # (spec, "--command", kind)
    rest: Tuple[str, ...] = ()                 # args after the spec, unchanged
    run_branch: Optional[str] = None
    start_point: Optional[str] = None
    recovery: Tuple[str, ...] = field(default_factory=tuple)


def _spec_of(facts: HandoffFacts) -> Optional[str]:
    for token in facts.args:
        if not token.startswith("-"):
            return token
    return None


def _is_single_task(facts: HandoffFacts) -> bool:
    if facts.kind != "implement":
        return False
    for token in facts.args[:2]:
        if token != "all" and _TASK_TOKEN.match(token):
            return True
    return False


def _under(path: str, root: str) -> bool:
    base = PurePosixPath(root) / ".claude" / "worktrees"
    try:
        PurePosixPath(path).relative_to(base)
    except ValueError:
        return False
    return PurePosixPath(path) != base


def _rest(facts: HandoffFacts, spec: Optional[str]) -> Tuple[str, ...]:
    args = tuple(facts.args)
    if spec is not None and spec in args:
        index = args.index(spec)
        return args[index + 1:]
    return args


def _recovery(
    facts: HandoffFacts,
    spec: Optional[str],
    run_args: Tuple[str, ...],
    rest: Tuple[str, ...],
    switch: bool,
) -> Tuple[str, ...]:
    main = facts.main_root or "<main checkout>"
    run = "ck run " + " ".join(shlex.quote(a) for a in (run_args or ((spec or "<spec>"), "--command", facts.kind)))
    if rest:
        run += " -- " + " ".join(shlex.quote(a) for a in rest)
    steps = []
    if switch and spec and facts.worktree_head:
        steps.append(f"git switch -c {shlex.quote(spec + '-run')} {shlex.quote(facts.worktree_head)}")
    steps.append(run)
    return (f"cd {shlex.quote(main)} && " + " && ".join(steps),)


def decide_handoff(facts: HandoffFacts) -> HandoffDecision:
    """S11. Never raises for any combination of facts."""
    main = facts.main_root
    if facts.kind not in HANDOFF_KINDS or _is_single_task(facts) or not facts.is_linked_worktree:
        return HandoffDecision("none", "no hand-off needed", main_checkout=main)
    spec = _spec_of(facts)
    rest = _rest(facts, spec)
    if main is None:
        return HandoffDecision(
            "refuse",
            "cannot find the main checkout of this worktree",
            rest=rest,
            recovery=_recovery(facts, spec, (), rest, False),
        )
    if not facts.cwd or not _under(facts.cwd, main):
        return HandoffDecision("none", "not a .claude/worktrees/ session", main_checkout=main)

    if spec is None:
        return HandoffDecision(
            "refuse",
            "no spec named in the arguments",
            main_checkout=main,
            rest=rest,
            recovery=_recovery(facts, None, (), rest, False),
        )
    run_args = (spec, "--command", facts.kind)
    if not facts.spec_in_main and not facts.spec_in_worktree:
        return HandoffDecision(
            "refuse",
            f"specs/{spec} exists at neither HEAD",
            main_checkout=main,
            run_args=run_args,
            rest=rest,
            recovery=_recovery(facts, spec, run_args, rest, False),
        )
    if facts.spec_same is True:
        return HandoffDecision(
            "handoff", "same spec at both HEADs", main_checkout=main, run_args=run_args, rest=rest
        )

    problem = None
    if facts.spec_same is None:
        problem = f"cannot tell whether specs/{spec} matches between the checkouts; run the commands below"
    elif facts.main_clean is not True:
        problem = "commit or stash the main checkout's changes, then run the commands below"
    elif facts.worktree_spec_dirty is not False:
        problem = f"commit the changes under specs/{spec} in this worktree, then run the commands below"
    elif facts.worktree_head is None:
        problem = "the worktree HEAD is unknown; fix the worktree, then run the commands below"
    elif facts.run_branch_at is not None and facts.run_branch_at != facts.worktree_head:
        problem = f"delete or rename the existing {spec}-run branch, then run the commands below"
    if problem is not None:
        return HandoffDecision(
            "refuse",
            problem,
            main_checkout=main,
            run_args=run_args,
            rest=rest,
            recovery=_recovery(facts, spec, run_args, rest, True),
        )
    return HandoffDecision(
        "handoff",
        "spec differs between the worktree and the main checkout",
        main_checkout=main,
        run_args=run_args,
        rest=rest,
        run_branch=f"{spec}-run",
        start_point=facts.worktree_head,
    )
