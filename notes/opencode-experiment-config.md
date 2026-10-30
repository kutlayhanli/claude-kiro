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
