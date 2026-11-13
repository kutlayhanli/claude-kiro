---
description: Run quick, throwaway experiments to settle what a plan depends on, and record the findings in EXPLORE.md
argument-hint: [feature-name or question]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, WebSearch, WebFetch, AskUserQuestion, Agent
---

Explore: $ARGUMENTS

# Spec-Explore: Find Out Before We Decide

This sits between talking (`/spec:plan`) and building (`/spec:create`, `/spec:implement`). Planning on assumptions produces a spec that is wrong in ways nobody notices until implementation. Here you check those assumptions with quick, dirty experiments: run the command, call the API, write a 20-line script, poke the real system. The output is evidence, not product code.

The output is `specs/[feature-name]/EXPLORE.md` (findings) plus throwaway probes in `specs/[feature-name]/explore/`.

**Rules for this command:**
- Probes are disposable. No tests-first, no structure, no polish. Fastest thing that answers the question wins.
- Never leave changes in product code. If a probe needs to patch real code, do it in a throwaway git worktree (`git worktree add ../<repo>-explore-<n> -b explore/<feature>-<n>`), and remove the worktree and branch when done. Keep only the diff, pasted into EXPLORE.md, if it's informative.
- Every finding cites what you ran and what you saw (command plus trimmed output). "Should work" and "the docs say" are not findings; mark those as **unverified**.
- Time-box each experiment. If one takes more than a few attempts, stop, record what you learned and what blocked you, and ask me whether to keep digging.
- Don't decide for me. Findings narrow the options; decisions still go through `/spec:plan`.

---

## Step 1: Pick the questions

1. Pick a kebab-case feature name. If `specs/[feature-name]/PLAN.md` exists, read it: its Open Questions, Risks, and any decision whose rationale rests on an assumption are your candidate questions. If `EXPLORE.md` exists, read it and continue from its unanswered questions.
2. If there is no PLAN.md, take the questions from `$ARGUMENTS` and a quick look at the code it would touch.
3. Turn each into a falsifiable question with a cheap experiment, for example:
   - "Does the Workflow runtime expose `Date.now`?" -> run a 3-line workflow and see.
   - "Is `git merge-tree` fast enough on this repo?" -> time it on the real history.
   - "Does the vendor API return X in the batch endpoint?" -> one curl with a real payload.
   - "Does approach A or B fit the existing code?" -> sketch both in a throwaway worktree, compare the diffs.
4. Show me the list (question, experiment, which decision it informs, rough cost) and ask with AskUserQuestion which to run. Recommend the ones whose answer would change a decision; skip ones that only confirm what we already believe.

## Step 2: Run the experiments

- Put probe scripts in `specs/[feature-name]/explore/` with a short name per question (`q1-date-now.js`, `q2-merge-tree-timing.sh`). Keep them runnable so I can rerun them.
- Run independent experiments in parallel: several Bash calls in one turn, or Agents (with `isolation: "worktree"` when they touch code) for longer ones. Keep only their conclusions here.
- Anything that costs money, sends data to an external service, or writes outside the repo: ask me first.
- After each experiment, append its finding to EXPLORE.md right away, so nothing is lost if we stop.

If a result surprises you, say so and tell me which plan assumption it breaks before running the next one. A surprise can make the remaining experiments moot.

## Step 3: Report and route

1. Summarize in under 10 lines: what we confirmed, what we disproved, what is still unknown, which decisions this changes.
2. Recommend one next step and ask me with AskUserQuestion:
   - **Back to planning** (`/spec:plan [feature-name]`): findings change or settle decisions. If PLAN.md exists, offer to add the findings to its Research Notes and turn answered Open Questions into notes that point at EXPLORE.md. Don't change its Decisions; that stays in `/spec:plan` with me.
   - **Straight to spec** (`/spec:create [feature-name]`): the plan is Decided and the findings confirm it.
   - **Just do it**: the experiments showed the change is small and clear enough that a full spec costs more than it saves. Say what the change is in a few lines; I decide whether to do it directly.
   - **Drop it**: the idea doesn't work or isn't worth it. Record why in EXPLORE.md so we don't explore it again.
3. Clean up: remove any throwaway worktrees and `explore/*` branches you created. Leave the probes in `specs/[feature-name]/explore/` unless I say to delete them.

---

## EXPLORE.md structure

```markdown
# Explore: [Feature Name]

**Updated:** [YYYY-MM-DD]
**Plan:** [specs/feature/PLAN.md, or none yet]
**Outcome:** In progress | Back to planning | Straight to spec | Just do it | Dropped

## Questions
| # | Question | Informs | Result |
|---|----------|---------|--------|
| Q1 | ... | D2 / Open Question | Confirmed / Disproved / Unknown / Not run |

## Findings

### Q1: [Question]
**Experiment:** `specs/[feature]/explore/q1-....sh` - what it does
**Ran:** `command`
**Saw:**
```
trimmed output
```
**Means:** what this tells us, and which decision or assumption it affects.

## Surprises
- Things we didn't ask about but learned, and why they matter.

## Still Unknown
- [ ] What we couldn't settle, and what it would take.
```

---

## Workflow summary

1. **Questions**: from PLAN.md's open questions and assumptions, or from my prompt; I pick which to run
2. **Experiments**: cheap, throwaway, parallel; product code only in a throwaway worktree; findings saved as they land
3. **Route**: back to `/spec:plan`, on to `/spec:create`, just do it, or drop it

**Output:** `specs/[feature-name]/EXPLORE.md` and `specs/[feature-name]/explore/`
