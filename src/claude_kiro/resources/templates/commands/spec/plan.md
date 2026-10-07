---
description: Discuss and decide what to build, interactively, and record the decisions in PLAN.md
argument-hint: [feature-description]
allowed-tools: Read, Write, Edit, Grep, Glob, WebSearch, WebFetch, AskUserQuestion, Agent, Bash(ck agents check:*)
---

Plan feature: $ARGUMENTS

## First: Model Check

The decisions made here shape every task after them, so this conversation should run on a strong model.

1. Find your model ID in your system prompt (for example "The exact model ID is claude-opus-5-5"), and your effort level if anything in your context states it.
2. Run `ck agents check --model <model id> --json`, adding `--effort <level>` only if you know it.
3. If it exits 0, or its `"ask"` is false, continue without mentioning it.
4. Otherwise ask me once with AskUserQuestion, before any other work: "This session runs <model, effort if known>. Planning works best on <min_model> at <min_effort> effort or higher. Switch first?" Options:
   - **Switch (recommended)**: tell me to run `/model` and choose <min_model> with <min_effort> effort or higher, then run this command again. Stop here.
   - **Continue on <current model>**: go on.
   You can't change the model yourself; don't try.
5. If `ck` isn't installed or the check fails to run, skip it and say so in one line.

The minimum is configurable: `ck agents set planning.min_model <model>`, `ck agents set planning.min_effort <level>`, or `ck agents set planning.ask false` to stop asking (add `--project` to set it for this project only).

# Spec-Plan: Decide Together, Then Write It Down

This is where we decide what to build. You and I talk it through: you bring research, options, and a recommendation; I make the calls. The output is `specs/[feature-name]/PLAN.md`, a decision record that `/spec:create` turns into requirements, design, and tasks without asking me again.

**Rules for this command:**
- Stay interactive. Ask before assuming. One topic at a time.
- Do not write requirements, design, tasks, or code here. Only PLAN.md.
- Every decision in PLAN.md must be one I made or explicitly accepted. Mark your own defaults as "accepted default" only after I agree.

---

## Step 1: Frame the problem (talk first)

1. Pick a kebab-case feature name (e.g. `user-authentication`). If `specs/[feature-name]/PLAN.md` already exists, read it and resume from its Open Questions instead of starting over.
2. Look around quickly before asking anything:
   - List `specs/` (and legacy `.claude/specs/` if present) for related or overlapping specs.
   - Grep/Glob for code this feature would touch. Note the 3-6 most relevant files.
3. Play the problem back to me in 3-5 lines: what you think I want, who it is for, what it touches in the codebase.
4. Ask me the questions whose answers change the shape of the work (use AskUserQuestion, up to 4 per call): goal and success signal, scope boundaries, hard constraints (deadline, compatibility, dependencies we can't add), and whether this evolves an existing spec.

Do not research yet. A wrong frame wastes the research.

## Step 2: Research what matters

Research only what the decisions need. Scale it to the problem:
- **Internal change** (refactor, wiring, tooling): mostly codebase reading. Find existing patterns, utilities, and constraints.
- **Unfamiliar domain or library choice**: also do 3-5 WebSearch queries for current best practices, real implementations, and known pitfalls. Prefer official docs and recent sources; extract the insight, not just the URL.

For broad sweeps, delegate to an Agent and keep only its conclusions here so our conversation stays focused.

Then create the first draft of `specs/[feature-name]/PLAN.md` (structure below) with Status `Discussing`, the framing, the research notes, and the decisions still open. Saving early means nothing is lost if we stop midway.

## Step 3: Decide, one decision at a time

List the decisions that must be made before a spec can be written, in dependency order (approach first, details later). Tell me the list, then go through it:

For each decision:
1. Lay out 2-4 real options (genuinely different, not variations). For each: how it works, where it shines, where it hurts, rough cost.
2. Give your recommendation and why, grounded in what you found in our codebase and research.
3. Ask with AskUserQuestion. Put your recommendation first, labelled "(Recommended)". Use `preview` for code or layout comparisons.
4. If I push back, pick "Other", or ask a question, discuss it. Don't rush to the next decision.
5. Record the outcome in PLAN.md right away: choice, rationale, rejected options and why.

New decisions often surface along the way (a choice opens a new question). Add them to the list and tell me.

Keep going until there are no open decisions that would change the requirements. Small details that `/spec:create` can reasonably settle can stay as "Delegated to spec", but say so and get my OK.

## Step 4: Confirm and hand off

1. Summarize the plan back to me in under 15 lines: what we're building, the key decisions, what's out of scope, the main risks.
2. Ask whether anything is missing or wrong. Apply changes.
3. Set Status to `Decided` and update the date.
4. Tell me: "Plan decided. Run `/spec:create [feature-name]` to write the spec. It runs as a workflow and ends with an adversarial review."

---

## PLAN.md structure

```markdown
# Plan: [Feature Name]

**Status:** Discussing | Decided
**Updated:** [YYYY-MM-DD]
**Evolves:** [specs/old-spec, or none]

## Problem
What we're solving, for whom, and how we'll know it worked.

## Scope
**In:**
- ...

**Out:**
- ...

## Constraints
Hard limits: compatibility, dependencies, performance budgets, deadlines.

## Decisions

### D1: [Decision question]
**Choice:** [what we picked]
**Why:** [rationale]
**Rejected:** [option] - [why not]; [option] - [why not]
**Decided by:** user | accepted default

### D2: ...

## Delegated to Spec
Details `/spec:create` may decide on its own, within the decisions above.

## Risks
- [Risk] -> [mitigation, or "accepted"]

## Open Questions
- [ ] [Anything still unresolved. Must be empty, or explicitly deferred, before Status is Decided.]

## Research Notes
### Codebase
- `path/to/file` - what's relevant there

### Sources
- [Title](url) - key insight
```

---

## Workflow summary

1. **Frame**: look at the code and existing specs, play it back, ask the shaping questions
2. **Research**: only what the decisions need; draft PLAN.md early
3. **Decide**: one decision at a time with AskUserQuestion; record each as it's made
4. **Confirm**: summary, final check, Status `Decided`
5. **Hand off**: `/spec:create [feature-name]`

**Output:** `specs/[feature-name]/PLAN.md`
