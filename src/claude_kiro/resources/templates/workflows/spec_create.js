export const meta = {
  name: 'spec-create',
  description: 'Write a spec (requirements, design, tasks) from PLAN.md, then adversarially review and revise it',
  whenToUse: 'Invoked by /spec:create (mode "create") and /spec:review (mode "review")',
  phases: [
    { title: 'Requirements', detail: 'EARS requirements from PLAN.md decisions' },
    { title: 'Design', detail: 'architecture grounded in the real codebase' },
    { title: 'Tasks', detail: 'dependency-ordered, parser-compatible task list' },
    { title: 'Adversarial Review', detail: '3 lenses, each finding challenged by a refuter' },
    { title: 'Revise', detail: 'apply confirmed fixes, write review.md' },
  ],
}

// args (passed by the slash command):
// {
//   mode: 'create' | 'review',
//   feature: 'user-auth',                 // kebab-case spec name
//   projectRoot: '/abs/path/to/project',
//   specDir: '/abs/path/to/project/specs/user-auth',
//   description: 'free-text feature description',
//   planPath: '/abs/.../specs/user-auth/PLAN.md' | null,
//   evolvesFrom: '/abs/.../specs/old-spec' | null,
//   context: 'orchestrator notes: relevant files, conventions, user answers',
//   date: 'YYYY-MM-DD'
// }
const A = args || {}
const MODE = A.mode === 'review' ? 'review' : 'create'
const ROOT = A.projectRoot
const DIR = A.specDir
const REQ = `${DIR}/requirements.md`
const DES = `${DIR}/design.md`
const TSK = `${DIR}/tasks.md`
const REVIEW = `${DIR}/review.md`

if (!ROOT || !DIR || !A.feature) {
  throw new Error('spec-create needs args.projectRoot, args.specDir and args.feature')
}

const BRIEF = `
Feature: ${A.feature}
Project root: ${ROOT}
Spec directory: ${DIR}
Date: ${A.date || '(unknown)'}
${A.planPath ? `Decision record (source of truth for scope and decisions): ${A.planPath}` : 'No PLAN.md exists. Work from the description and context below and make every assumption explicit.'}
${A.evolvesFrom ? `This spec evolves an existing spec at: ${A.evolvesFrom}` : ''}

Description:
${A.description || '(see PLAN.md)'}

Orchestrator context:
${A.context || '(none)'}
`.trim()

const PLAN_RULE = A.planPath
  ? `Read ${A.planPath} first. Every decision in its Decisions section is binding: do not reopen, contradict, or silently drop one. Items it marks out of scope stay out of scope. If a decision is impossible to honor, say so in "concerns" and do not invent a replacement.`
  : 'There is no PLAN.md, so record every non-obvious choice you make in "assumptions".'

const WRITE_RESULT = {
  type: 'object',
  properties: {
    path: { type: 'string' },
    summary: { type: 'string', description: '2-4 sentences: what the document commits to' },
    assumptions: { type: 'array', items: { type: 'string' } },
    concerns: { type: 'array', items: { type: 'string' }, description: 'conflicts or risks the user should know about' },
  },
  required: ['path', 'summary', 'assumptions', 'concerns'],
}

// ---------------------------------------------------------------------------
// Authoring phases (create mode only)
// ---------------------------------------------------------------------------

const authored = {}

if (MODE === 'create') {
  phase('Requirements')
  authored.requirements = await agent(`
You are writing the requirements document of a spec.

${BRIEF}

${PLAN_RULE}

Explore the codebase enough to know what already exists. Then write ${REQ} with this structure:

# Feature: [Name]

## Overview
What it does and why. Link to PLAN.md if it exists.

## User Stories
### Story N: [Title]
**As a** [user type] **I want** [goal] **So that** [benefit]

**Acceptance Criteria:**
- N.1 WHEN [condition/event], THE SYSTEM SHALL [observable behavior]
- N.2 WHEN [error condition], THE SYSTEM SHALL [error handling]
(Number every criterion N.M so design and tasks can trace to it.)

## Specification Heritage   (only if this evolves an existing spec)
Evolves From / Preserves / Supersedes / Evolution Reason

## Non-Functional Requirements
Only the ones that matter here, each with a concrete, checkable target.

## Constraints

## Out of Scope
Explicit list, including everything PLAN.md excludes.

## Assumptions

Rules:
- Every criterion uses EARS and is testable by an automated or a clearly described manual check.
- Cover the happy path, error paths, boundaries, and invalid input.
- Describe WHAT, not HOW. No file names or class names.
${A.evolvesFrom ? `- Evolution: read ${A.evolvesFrom}. Add the Heritage section. Then, in the OLD spec, set the tasks.md status line to "EVOLVED - See: ${A.feature}", mark superseded tasks EVOLVED, and write ${A.evolvesFrom}/evolution.md (what changed, why, lessons, what remains valid).` : ''}

Write the file, then return the structured result.`, { label: 'write requirements.md', phase: 'Requirements', schema: WRITE_RESULT })

  phase('Design')
  authored.design = await agent(`
You are writing the design document of a spec.

${BRIEF}

${PLAN_RULE}

Read ${REQ}. Then study the codebase thoroughly: find the modules this touches, read them, and follow existing patterns and naming. Write ${DES} with:

# Design: [Feature Name]

## Overview
How this fits the existing system, and which PLAN.md decisions shape it.

## Components
### Existing Components to Modify
- \`real/path/in/repo\` - what changes and why
### New Components
- \`proposed/path\` - responsibility

## Interfaces and Data Models
Signatures, types, schemas in the project's own language. Every field typed and explained.

## Data Flow
A Mermaid diagram of the main flow (sequence or flowchart).

## Error Handling
One entry per error criterion in requirements.md, referenced by number.

## Testing Strategy
Which test types, where they live (real test directories), and which criteria each covers.

## Security, Performance, Migration
Only the sections that apply.

## Requirements Traceability
| Criterion | Design element |
Every criterion from requirements.md appears here.

Rules:
- Use real paths. Before naming an existing file, confirm it exists. Mark new files as new.
- Match the conventions you actually see in the code (framework, error style, test layout).
- No component without a requirement driving it.

Write the file, then return the structured result.`, { label: 'write design.md', phase: 'Design', schema: WRITE_RESULT })

  phase('Tasks')
  authored.tasks = await agent(`
You are writing the implementation task list of a spec.

${BRIEF}

Read ${REQ} and ${DES}. Write ${TSK}. Hooks and tooling parse this file, so follow the format exactly:

# Implementation Tasks: [Feature Name]

**Status:** Not Started
**Spec:** [requirements.md](requirements.md) · [design.md](design.md)${A.planPath ? ' · [PLAN.md](PLAN.md)' : ''}

## Task Breakdown

### Task 1: [Action-oriented title]
**Status:** Not Started
**Description:** What to do, in enough detail that an engineer new to the repo can start without asking.
**Requirements:** 1.1, 1.2
**Files:**
- \`path/to/file\` - specific change
- \`path/to/test_file\` - tests to add

**Acceptance:**
- [ ] [Concrete check derived from the referenced criteria]
- [ ] Tests written and passing

**Dependencies:** None | Task N, Task M
**Complexity:** Low | Medium | High

---

(repeat for each task)

## Dependency Graph
A Mermaid graph TD of task dependencies.

## Parallel Groups
Waves of tasks that can run concurrently via /spawn-worktree, e.g. "Wave 1: Tasks 1, 2 · Wave 2: Task 3 (after 1)". Tasks in one wave must not edit the same files.

Rules:
- Headers MUST be "### Task N: Title" and file lines MUST be "- \`path\` - note" under **Files:**.
- Every acceptance criterion in requirements.md is covered by at least one task.
- Every task names its files and its tests. No task references a component absent from design.md.
- Prefer small tasks (one focused change set each). Order by dependency.

Write the file, then return the structured result.`, { label: 'write tasks.md', phase: 'Tasks', schema: WRITE_RESULT })

  const missing = Object.entries(authored).filter(([, v]) => !v).map(([k]) => k)
  if (missing.length) {
    log(`Authoring failed for: ${missing.join(', ')}. Stopping before review.`)
    return { mode: MODE, feature: A.feature, specDir: DIR, error: `authoring failed: ${missing.join(', ')}`, authored }
  }
}

// ---------------------------------------------------------------------------
// Adversarial review: each lens finds defects, then a separate agent tries to
// refute every finding. Only findings that survive refutation are acted on.
// ---------------------------------------------------------------------------

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', description: 'short unique id, e.g. R1, D3, T2' },
          file: { type: 'string', enum: ['requirements.md', 'design.md', 'tasks.md', 'PLAN.md'] },
          location: { type: 'string', description: 'section, story, or task number' },
          severity: { type: 'string', enum: ['critical', 'major', 'minor'] },
          problem: { type: 'string' },
          evidence: { type: 'string', description: 'quote from the spec and/or path:line in the code that proves it' },
          fix: { type: 'string', description: 'the concrete edit that resolves it' },
          needsUserDecision: { type: 'boolean', description: 'true only if fixing it requires a product/scope choice the spec and PLAN.md cannot settle' },
          question: { type: 'string', description: 'the decision to put to the user, when needsUserDecision is true' },
        },
        required: ['id', 'file', 'location', 'severity', 'problem', 'evidence', 'fix', 'needsUserDecision'],
      },
    },
  },
  required: ['findings'],
}

const VERDICTS = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          refuted: { type: 'boolean' },
          reason: { type: 'string' },
        },
        required: ['id', 'refuted', 'reason'],
      },
    },
  },
  required: ['verdicts'],
}

const LENSES = [
  {
    key: 'fidelity',
    prefix: 'R',
    focus: `PLAN FIDELITY AND REQUIREMENTS QUALITY.
- ${A.planPath ? `Does the spec honor every decision in ${A.planPath}? Anything reopened, contradicted, dropped, or scope-crept?` : 'Are the assumptions reasonable and stated, and does the spec match the description?'}
- Is every acceptance criterion EARS, observable, and testable? Flag vague words ("fast", "secure", "user-friendly") with no target.
- Missing error paths, boundaries, invalid input, concurrency, empty states?
- Criteria that contradict each other, or implementation detail leaking into requirements?`,
  },
  {
    key: 'grounding',
    prefix: 'D',
    focus: `DESIGN GROUNDED IN THE REAL CODEBASE.
- Open every existing path design.md names. Does it exist? Does the described change fit what the file actually contains?
- Do proposed interfaces collide with or duplicate existing ones? Does the design ignore a utility or pattern the repo already has?
- Does every requirement criterion map to a design element, and every design element to a criterion?
- Would the error-handling or data-flow description break under the code's real constraints (sync vs async, transactions, auth, config)?`,
  },
  {
    key: 'implementability',
    prefix: 'T',
    focus: `TASKS AN ENGINEER CAN EXECUTE.
- Is every requirement criterion covered by a task? List any orphans.
- Are dependencies correct and acyclic? Is any task in a parallel wave editing the same file as another task in that wave?
- Does each task name files and tests, and is it small enough to finish in one sitting?
- Format: "### Task N: Title" headers and "- \`path\` - note" file lines (tooling parses these).
- Pretend you are implementing Task 1 and the riskiest task right now. Where would you get stuck or have to guess?`,
  },
]

phase('Adversarial Review')
const reviewed = await pipeline(
  LENSES,
  lens => agent(`
You are an adversarial reviewer of a spec. Assume it is wrong until the files prove otherwise. Your job is to find concrete defects that would cause failed implementation, rework, or a feature that does not do what was decided.

${BRIEF}

Spec files: ${REQ}, ${DES}, ${TSK}${A.planPath ? `, decision record ${A.planPath}` : ''}.

Your lens: ${lens.focus}

Rules:
- Read the spec files and the relevant code yourself. Every finding needs evidence: a quote from the spec, or a path:line from the code.
- Report real defects, not style preferences or rewording. Fewer, solid findings beat many weak ones.
- Give each finding an id starting with "${lens.prefix}" and a concrete fix.
- Set needsUserDecision only when the fix requires a product or scope choice the spec and PLAN.md cannot settle.
- Do not edit any file.`, { label: `review: ${lens.key}`, phase: 'Adversarial Review', schema: FINDINGS }),
  (review, lens) => {
    if (!review) return null // reviewer died; counted as a lost lens below
    const findings = review.findings || []
    if (!findings.length) return { lens: lens.key, findings: [], verdicts: [] }
    return agent(`
You are a skeptic. A reviewer raised the findings below against a spec. Try to REFUTE each one.

${BRIEF}

Spec files: ${REQ}, ${DES}, ${TSK}${A.planPath ? `, decision record ${A.planPath}` : ''}.

Findings (JSON):
${JSON.stringify(findings, null, 2)}

For each finding, check its evidence against the actual files and code. Mark refuted=true when:
- the evidence is wrong or the spec already handles it elsewhere,
- it is a style preference, rewording, or speculative "might be nice",
- it contradicts a binding PLAN.md decision,
- the proposed fix would make the spec worse.
Mark refuted=false only when the defect is real and the fix is sound. When unsure, refute.
Do not edit any file.`, { label: `refute: ${lens.key}`, phase: 'Adversarial Review', schema: VERDICTS })
      .then(v => ({ lens: lens.key, findings, verdicts: (v && v.verdicts) || null }))
  },
)

const confirmed = []
const refuted = []
const unverified = []
for (const r of reviewed.filter(Boolean)) {
  if (r.verdicts === null) {
    // Refuter died; keep the findings visible but do not auto-apply them.
    unverified.push(...r.findings)
    continue
  }
  const byId = Object.fromEntries(r.verdicts.map(v => [v.id, v]))
  for (const f of r.findings) {
    const v = byId[f.id]
    if (v && v.refuted) refuted.push({ ...f, refutation: v.reason })
    else confirmed.push({ ...f, verification: v ? v.reason : 'no verdict returned' })
  }
}
const lensesLost = LENSES.length - reviewed.filter(Boolean).length
if (lensesLost) log(`${lensesLost} review lens(es) failed to return; coverage is partial.`)
if (unverified.length) log(`${unverified.length} finding(s) could not be verified and will not be auto-applied.`)

const toFix = confirmed.filter(f => !f.needsUserDecision)
const forUser = confirmed.filter(f => f.needsUserDecision)
log(`Review: ${confirmed.length} confirmed (${toFix.length} to fix, ${forUser.length} need your decision), ${refuted.length} refuted.`)

// ---------------------------------------------------------------------------
// Revise: apply confirmed fixes and write the review record.
// ---------------------------------------------------------------------------

phase('Revise')
const revision = await agent(`
You are revising a spec after adversarial review.

${BRIEF}

Spec files: ${REQ}, ${DES}, ${TSK}.

1. Apply each of these confirmed findings by editing the spec files. Keep the documents consistent with each other (if you change a criterion, update design traceability and the covering task). Keep the tasks.md format intact ("### Task N: Title", "- \`path\` - note").
${JSON.stringify(toFix, null, 2)}

2. Do NOT resolve these; they need the user's decision. Leave the spec as is for them:
${JSON.stringify(forUser.map(f => ({ id: f.id, question: f.question || f.problem })), null, 2)}

3. Write ${REVIEW}:

# Spec Review: ${A.feature}

**Date:** ${A.date || ''}
**Mode:** ${MODE}
**Result:** [Ready | Ready after decisions | Needs work]

## Fixed
| ID | File | Severity | Problem | Fix applied |

## Needs Your Decision
| ID | Question |

## Refuted
| ID | Problem | Why it was refuted |
${JSON.stringify(refuted.map(f => ({ id: f.id, problem: f.problem, why: f.refutation })), null, 2)}

## Unverified
${unverified.length ? JSON.stringify(unverified.map(f => ({ id: f.id, problem: f.problem })), null, 2) : 'None'}

## Parallel Groups
Copy the waves from tasks.md.

Return the structured result.`, {
  label: 'revise spec + write review.md',
  phase: 'Revise',
  ...(toFix.length ? {} : { effort: 'low' }),
  schema: {
    type: 'object',
    properties: {
      fixed: { type: 'array', items: { type: 'string' }, description: 'ids applied' },
      notFixed: {
        type: 'array',
        items: { type: 'object', properties: { id: { type: 'string' }, reason: { type: 'string' } }, required: ['id', 'reason'] },
      },
      result: { type: 'string', enum: ['Ready', 'Ready after decisions', 'Needs work'] },
      parallelGroups: { type: 'string' },
    },
    required: ['fixed', 'notFixed', 'result', 'parallelGroups'],
  },
})

return {
  mode: MODE,
  feature: A.feature,
  specDir: DIR,
  files: MODE === 'create' ? [REQ, DES, TSK, REVIEW] : [REVIEW],
  authored: MODE === 'create'
    ? Object.fromEntries(Object.entries(authored).map(([k, v]) => [k, { summary: v.summary, assumptions: v.assumptions, concerns: v.concerns }]))
    : null,
  review: {
    result: revision ? revision.result : 'Needs work',
    fixed: revision ? revision.fixed : [],
    notFixed: revision ? revision.notFixed : toFix.map(f => ({ id: f.id, reason: 'revise step failed' })),
    openQuestions: forUser.map(f => ({ id: f.id, file: f.file, location: f.location, question: f.question || f.problem, fix: f.fix })),
    refutedCount: refuted.length,
    unverified: unverified.map(f => ({ id: f.id, problem: f.problem })),
    lensesLost,
  },
  parallelGroups: revision ? revision.parallelGroups : null,
}
