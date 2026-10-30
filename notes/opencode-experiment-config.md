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
`ck bench import` is still the collection format; the native record is kept under
`bench/runs/opencode-state/<arm>/claude-kiro/opencode/runs/`.

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
harnesses; put any deliberate deviation in both configs, not just one.

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
