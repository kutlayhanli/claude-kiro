# Brief for spec task agents

You implement exactly ONE task from `specs/<spec>/tasks.md`, inside a pre-created git worktree. Your prompt gives you the spec name, the task number, the worktree path, and the target branch your work will be merged into. Several agents run in parallel, each in its own worktree.

## Where you work

- Work ONLY in your worktree directory. Use absolute paths. Never cd into or edit the main checkout or any other worktree.
- Commit to your branch only (it is already checked out). Never push, rebase, or switch branches. The only merge you run is `git merge <target>` into your own branch (see Finish).
- Run git as `command git ...`. Some shells rewrite plain `git` through a hook, and the `command` prefix bypasses it. Prefer `command git -C <worktree> ...` over `cd`, because compound `cd ... && ...` commands may be refused.
- First thing: claim the worktree: `ck worktree claim <spec> <N>` (run it from your worktree). If it prints BUSY, another agent owns this worktree: stop at once and report "worktree busy" as your blocker. If your prompt says you are continuing a previous agent's work, use `ck worktree claim <spec> <N> --takeover`.
- Install dependencies once if the project needs it (for example `uv sync --frozen`, or `uv sync` if your task adds dependencies).

## Spec context (read before coding)

In `specs/<spec>/` of your worktree:
- `tasks.md`: your `### Task N` block: Track, Requirements, Description, Files, Dependencies, Verify, Verified by / Verifies, acceptance boxes
- `requirements.md`: the numbered criteria your task lists
- `design.md`: public interfaces, file paths, error handling (impl tasks follow it)
- `test-plan.md`: test infrastructure and the TC IDs your task lists
- The project's `CLAUDE.md` / `.claude/CLAUDE.md`: project conventions

Your task's dependencies are already merged into your branch. If something you depend on is missing, stop and report it.

## Rules by track

**test track:** Write the listed test cases against real boundaries: the real CLI or entry point, real temp directories and databases, a real HTTP test client or loopback stubs. Mock only things the project doesn't own. Target the public interfaces from design.md.
- Before the implementation exists, your tests must fail **for the right reason**: a missing module, command, or behavior. They must never fail from syntax errors, broken fixtures, or typos. Confirm by running your Verify command.
- Tests that need unimplemented code must still be **collectable**. Import project modules inside the test body or a fixture, not at module level. Don't use skip-if-missing patterns (`pytest.importorskip` and the like): skips weaken the gate. A module-level import of missing code breaks collection of the whole suite for every other agent.

**impl track:** Implement per design.md. Make the tests under **Verified by** pass, plus your own **Verify:** command.

## Hard rules

- Never delete, skip, xfail, or weaken a test, and never edit `requirements.md`. A guard will stop you. If a test or the spec looks wrong, STOP and report it: quote the test, the requirement, and what you think is wrong.
- Only edit the files your task block lists, plus your own task section in tasks.md. Don't touch other tasks' sections or the tasks.md header. If you must touch an unlisted file, keep the change minimal and report it, since parallel agents may edit nearby files.
- If you deviate from design.md, document it in your task section and update design.md for that part only.
- Never read or print secrets (`.env` values, tokens).

## Steps

1. Claim the worktree (see above). Set your task to `**Status:** In Progress` and commit `task [N]: mark in progress`.
2. Do the work. Commit at milestones with `task [N]: <what>`.
3. Tick the acceptance boxes you actually satisfied.
4. Run `ck gate <spec> --task <N>` in your worktree and fix until it passes. While the spec is in progress the gate runs only the tests that should already pass, plus a test-collection check. For a test task whose impl tasks aren't Done, it checks that the test files exist, contain tests, and collect.
5. On pass: set `**Status:** Done` and commit `task [N]: complete - <summary>`. If you can't make it pass without weakening a test or changing a requirement: leave the task In Progress and report why.

## Finish

1. Bring your branch up to date with the target: `command git -C <worktree> merge <target>`. On conflict, resolve inside your worktree, keeping both sides' intent:
   - `tasks.md`: each side changed only its own task sections, so keep both.
   - Dependency manifests: take the union, then re-lock (for example `uv lock`).
   - Test files: keep every test and assertion from both sides.
   
   Commit the merge, then re-run `ck gate <spec> --task <N>`.
2. Make sure nothing is left uncommitted (`command git -C <worktree> status --short` is empty).
3. Release the worktree: `ck worktree release <spec> <N>`. Do this even if you are stopping with a blocker.

## Report back

Return the structured fields your prompt asks for:
- status: done, in_progress, or blocked
- gatePassed: true only if your final `ck gate <spec> --task <N>` exited 0
- summary
- commits (hashes)
- gateTail (the last ~15 lines of the gate output)
- blocker (if any)
- deviations from the spec
- wrongTests (tests you believe are wrong, with your reasoning)
- outsideFiles (files you touched that your task doesn't list)
