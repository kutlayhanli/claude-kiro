# Upgrading to 0.2

This guide is for machines and repositories already set up with claude-kiro 0.1.x.

## What changes

| Area | 0.1 | 0.2 |
|---|---|---|
| Spec location | `.claude/specs/<feature>/` (every write needs approval) | `specs/<feature>/` at the project root |
| `/spec:plan` | Research report, then ExitPlanMode | Interactive: decide one question at a time, PLAN.md is the decision record |
| `/spec:create` | Three phases with approval stops | A workflow: requirements, then design and test plan in parallel, then test and impl task tracks, then adversarial review (needs a Claude Code version with the Workflow tool) |
| Testing | Testing section inside design.md | `test-plan.md`, integration-first; test tasks are written before the code they verify |
| Done | Agent decides | `ck gate` decides; a Stop hook blocks a Done claim that fails it |
| Tests | Unprotected | A PreToolUse guard asks before tests are weakened or deleted, or requirements are edited mid-implementation |
| Hooks | One PostToolUse hook; `ck init` replaced every PostToolUse hook and wrote a timeout of `5000` (read by Claude Code as seconds) | Four hooks, merged alongside your other hooks, timeouts in seconds |

Two parts are upgraded separately:

- **Each machine**: the `ck` tool and the global files in `~/.claude/`.
- **Each repository**: the files `ck init` wrote into the repo, plus the hooks in `.claude/settings.local.json`. Most of the repo files travel through git; the hooks file is local to each clone.

## 1. Each machine

### Install the new `ck`

PyPI only has the upstream 0.1.0, so install from the fork:

```bash
uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro@feat/specs-dir-and-workflow-create"
```

Once the branch is merged, use `@master` instead. If you keep a local clone and want `ck` to follow it:

```bash
uv tool install --force --editable /path/to/claude-kiro
```

Check it worked: `ck --help` should list `gate`, `migrate`, and `upgrade`.

### Update the global files in `~/.claude/`

`ck setup` never overwrites existing files without `--force`, and `--force` replaces the whole file. Look first:

```bash
ck setup --diff
```

- **Your `~/.claude/CLAUDE.md` is customized** (most likely): don't use `--force`. Paste this block in instead, replacing any older claude-kiro section:

  ```markdown
  ## Spec-Driven Development (claude-kiro)

  1. `/spec:plan` - Discuss and decide the approach with me, interactively (writes PLAN.md)
  2. `/spec:create` - Workflow that writes requirements, design, test plan, and tasks, then adversarially reviews them
  3. `/spec:implement` - Implement a test-track or impl-track task; Done only when `ck gate` passes
  4. `/spawn-worktree` - Run a wave of tasks in parallel with git worktree isolation

  - Specs live in `specs/[feature-name]/` at the project root (not in `.claude/`)
  - Each spec has: PLAN.md, requirements.md, design.md, test-plan.md, tasks.md, review.md
  - Tests are integration-first and written before the code they verify; never weaken a test or edit requirements to get a pass
  - Commit messages for spec tasks use the `task [N]:` prefix
  ```

- **You never edited it:** `ck setup --force` is fine.
- **The spawn-worktree skill is missing or unchanged:** `ck setup` (without `--force`) installs anything missing.

### Restart Claude Code

Hooks and slash commands are loaded when a session starts. Close and reopen any running sessions after upgrading.

## 2. Each repository

Run this from the repository root:

```bash
ck upgrade --dry-run   # shows the plan, writes nothing
ck upgrade
```

`ck upgrade`:

- refreshes the files ck owns: `.claude/commands/spec/*`, `.claude/commands/spawn-worktree.md`, `.claude/output-styles/spec-driven.md`, and adds `.claude/workflows/spec-create.js`;
- merges the four hooks into `.claude/settings.local.json`, keeping other tools' hooks and permissions, and fixing the old `5000` timeout;
- creates `specs/ck.json` with a detected test command;
- moves `.claude/specs/*` to `specs/` with `git mv`, so history follows, and rewrites `.claude/specs/` references inside them. Use `--no-migrate` to skip this step.

It never touches `.claude/CLAUDE.md` or your spec content. If you customized a ck command file that git doesn't track, it is saved as `<file>.bak` before being replaced. Tracked files can be reviewed with `git diff`.

Then:

1. **Check the verify command.** Open `specs/ck.json` and make sure `"verify"` runs your test suite, for example `["uv run pytest -q"]`, `["npm test --silent"]`, or `["go test ./..."]`. The gate is only as good as this command. Leave it empty only if every task has its own `**Verify:**` line.
2. **Review and commit:**
   ```bash
   git status && git diff .claude specs
   git add .claude/commands .claude/workflows .claude/output-styles specs
   git commit -m "Upgrade claude-kiro to 0.2"
   ```
3. **Restart Claude Code sessions** in this repo, then run `ck doctor`.

### Other clones of the same repository

After pulling the upgrade commit, the commands, workflow, `specs/ck.json`, and moved specs arrive through git. The hooks don't, because `.claude/settings.local.json` is local to each clone. Run `ck upgrade` once in every other clone, on every machine. It will only add the hooks.

To share hooks through git instead, copy the `"hooks"` block that `ck hook config` prints into `.claude/settings.json` (the shared, committed settings file). Every clone then gets the hooks as soon as it pulls, but every clone also needs the 0.2 `ck` on its PATH.

### Specs already in progress

- **They keep working.** The task parser also reads the older format (`### Task1:`, and `✅ COMPLETE` in the title counts as Done). Tasks without `**Track:**` are treated as implementation tasks.
- **The gate applies to them.** When Claude marks one of their tasks Done, the acceptance boxes are checked and the verify commands run. For older specs without `**Verify:**` or `**Verified by:**` lines, only the project-wide verify commands run.
- **Optional: add a test plan.** `/spec:review <name>` runs the new four-lens review. On a spec without `test-plan.md`, it flags the gap and writes one. This is worth doing for specs with significant work still ahead.

### Tuning or switching things off

All of these settings live in `specs/ck.json`:

```json
{
  "verify": ["uv run pytest -q"],
  "verify_timeout": 540,
  "guard": { "tests": "ask", "requirements": "ask" },
  "base": null
}
```

- `guard.tests` and `guard.requirements` each take `"ask"` (prompt me), `"deny"` (block and tell Claude why), or `"off"`.
- `base` is the git ref the tamper check compares against. Leave it `null` to use the merge-base with `origin/HEAD`, `main`, or `master`.
- To list which files count as tests, add `"tests": {"dirs": [...], "files": [...]}`.
- To disable the gate entirely, remove the `Stop` and `SubagentStop` entries from `.claude/settings.local.json`.

## Rolling back

```bash
uv tool install --force claude-kiro==0.1.0                          # old ck
git revert <upgrade-commit>                                          # repo files and spec move
```

Then remove the `PreToolUse`, `Stop`, and `SubagentStop` entries that run `ck --hook ...` from `.claude/settings.local.json`.

## Checklist

**Each machine**
- [ ] `uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro@feat/specs-dir-and-workflow-create"`
- [ ] `ck setup --diff`, then paste the snippet or run `ck setup --force`
- [ ] Restart Claude Code

**Each repository** (once)
- [ ] `ck upgrade --dry-run`, then `ck upgrade`
- [ ] Check `"verify"` in `specs/ck.json`
- [ ] Review the diff and commit `.claude/commands .claude/workflows .claude/output-styles specs`
- [ ] Restart sessions, then run `ck doctor`

**Each additional clone**
- [ ] `git pull`, then `ck upgrade`, then restart sessions
