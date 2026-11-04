# OpenCode benchmark config

Template for `ck bench run --harness opencode`. Copy next to your bench checkout and fill `repo`,
`specs` and `opencode.plugin`.

```jsonc
{
  "repo": "/absolute/path/to/the/base/checkout",
  "base": "main",
  "seed": 7,
  "reps": 3,
  "bench_dir": "/absolute/bench/runs",
  "results_dir": "/absolute/bench/results",
  "ck": ["ck"],
  "timeout_minutes": 120,
  "env": {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_ADDOPTS": "-p no:cacheprovider"
  },
  "opencode": {
    "plugin": "/absolute/path/to/opencode",
    "bin": "opencode"
  },
  "specs": [
    {
      "name": "your-spec",
      "hidden_tests": "/absolute/bench/hidden/your-spec",
      "invariants": "/absolute/bench/invariants",
      "test_cmd": ["python", "-m", "pytest"]
    }
  ],
  "configs": [
    {
      "name": "sonnet-5-5",
      "model": "opencode/claude-sonnet-5-5",
      "agents": {
        "implementer.model": "sonnet",
        "test_writer.model": "sonnet",
        "reviewer.model": "sonnet",
        "fixer.model": "sonnet",
        "resolver.model": "sonnet",
        "orchestrator.model": "sonnet"
      }
    },
    {
      "name": "deepseek-free",
      "model": "opencode/deepseek-v4.1-flash-free",
      "agents": {
        "implementer.model": "sonnet",
        "test_writer.model": "sonnet",
        "reviewer.model": "sonnet",
        "fixer.model": "sonnet",
        "resolver.model": "sonnet",
        "orchestrator.model": "sonnet"
      }
    },
    {
      "name": "muse-contributor",
      "model": "opencode-go/muse-spark-1.3-contributor",
      "agents": {
        "implementer.model": "sonnet",
        "test_writer.model": "sonnet",
        "reviewer.model": "sonnet",
        "fixer.model": "sonnet",
        "resolver.model": "sonnet",
        "orchestrator.model": "sonnet"
      }
    }
  ]
}
```

Every role is pinned to the same arm model for the first pass: the roles use the ck alias `sonnet`,
and the runner writes `opencode.models.<alias> = <arm model>` into the clone's `specs/ck.json`. That
is the OpenCode equivalent of the all-Opus/all-Sonnet Claude arms. Role ladders come later by mapping
`opus`/`haiku` to different refs in `opencode.models`.

```sh
# E0 first: if this fails, stop; the cheap arms cannot be interpreted.
ck bench run bench-opencode.json --harness opencode --only your-spec--sonnet-5-5--r1

# Then the full 3x matrix.
ck bench run bench-opencode.json --harness opencode
ck bench score /absolute/bench/results
```

The runner sets `CK_RUN_HEADLESS=1`, uses a private `XDG_STATE_HOME` outside the clone, and marks an
arm fail-closed on a timeout, a non-`done` record, a guard denial/post-check, or a failure label.
Both configs above set `PYTHONDONTWRITEBYTECODE=1` and disable pytest's cache: a task worktree with
untracked test artefacts (`__pycache__/`, `.pytest_cache/`) is "busy" and `ck` refuses to merge it,
which would look like a model failure. If the repo under test has no `.gitignore` for those paths,
either add one to the base or keep this env; apply the same setting to the Claude baseline so both
harnesses see the same rule.
`ck bench import` is still the collection format; the native record is kept under
`bench/runs/opencode-state/<arm>/claude-kiro/opencode/runs/`.

## Model availability (checked 2026-10-30)

| Ref | Status |
|---|---|
| `opencode/claude-sonnet-5-5` | HTTP 402, account unfunded — the intended E0 baseline |
| `opencode/deepseek-v4.1-flash-free` | HTTP 429, `retryAfterMs` ~19.7 M — E1 |
| `opencode-go/muse-spark-1.3-contributor` | HTTP 400, workspace must allow train-on-request-data — E2 |
| `opencode-go/deepseek-v4-pro` | works; live demo arm merged 1/1 in 5.09 min, $0.08 list-price estimate (provider reported $0.00) |
| `opencode-go/claude-haiku-5-5` | works, but too weak for E0 |
| `opencode-go/beam-free` | works; used for the repeated harness E2E (1.1 min) |

Use `opencode models | grep <name>` to confirm a ref before adding an arm. A 402 is a funding
blocker, a 429 is a throttle (retry later), and a 400 privacy error needs a workspace setting; none
of those is a port failure.

## Claude Code baseline

Run the same specs on the same base with `ck bench` on Claude Code. Use a separate `results_dir`;
keep the spec names and the config names (`sonnet-5-5`, `opus-5-5`) identical so the paired rows line
up when the two `score.md` files are compared.

```jsonc
{
  "repo": "/absolute/path/to/the/base/checkout",
  "base": "main",
  "seed": 7,
  "reps": 3,
  "bench_dir": "/absolute/bench/claude-runs",
  "results_dir": "/absolute/bench/results-claude",
  "ck": ["ck"],
  "timeout_minutes": 120,
  "env": {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_ADDOPTS": "-p no:cacheprovider"
  },
  "specs": [
    { "name": "your-spec", "hidden_tests": "/absolute/bench/hidden/your-spec",
      "invariants": "/absolute/bench/invariants", "test_cmd": ["python", "-m", "pytest"] }
  ],
  "configs": [
    {
      "name": "sonnet-5-5",
      "agents": {
        "planning.min_model": "sonnet",
        "implementer.model": "sonnet", "implementer.escalate": "sonnet",
        "implementer.risky_model": "sonnet",
        "test_writer.model": "sonnet", "test_writer.escalate": "sonnet",
        "reviewer.model": "sonnet", "reviewer.risky_model": "sonnet",
        "fixer.model": "sonnet", "fixer.escalate": "sonnet",
        "orchestrator.model": "sonnet", "resolver.model": "sonnet"
      },
      "run_args": []
    }
  ]
}
```

Every role is pinned to the same alias, matching the OpenCode arm's all-roles-on-one-model setup.
`run_args` stays empty so the concurrency, review rounds and gate cadence are the defaults on both
harnesses; put any deliberate deviation in both configs, not just one. The Claude arm requires a
ck-initialized repo (`.claude/` and the spec plugin); `ck bench` runs `ck upgrade` in the clone and
aborts otherwise. The OpenCode arm does not need `.claude/`, so use one base repo that is initialized
for Claude Code and clone it for both configs.

```sh
ck bench run bench-claude.json
ck bench score /absolute/bench/results-claude --config bench-claude.json
```

## Comparing the two harnesses

1. **E0 gate first, per harness.** Run one strong-model arm on each. If either fails for a port
   reason, fix it; do not compare cheap arms until both E0s pass.
2. **Same inputs.** Same repo SHA, same specs, same hidden suites/invariants, same reps, same seed and
   the same agent settings. Only the harness and model refs differ.
3. **Match arms by `(spec, config, rep)`.** `ck bench score` reports per-arm hidden pass rate, merged
   rate, cost, wall time, escalations and invariants; compare those paired rows, not the aggregate
   headline.
4. **Watch the cost basis.** Claude arms price transcripts; OpenCode arms use `session.get().cost` and
   price free models at $0.00. A $0.00 OpenCode arm is a free model, not a cheap paid one.
5. **Label every failure** `model | port | throttle | spec | env`. A `port` failure invalidates the
   comparison for that arm; rerun after the fix.
6. **Keep artefacts outside the worktree** and never score against logs the model under test could
   write. Hidden suites stay outside the clone.
