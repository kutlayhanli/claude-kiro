export const meta = {
  name: 'spec-create',
  description: 'Write a spec (requirements, then design and test plan in parallel, then two task tracks), then adversarially review and revise it',
  whenToUse: 'Invoked by /spec:create (mode "create") and /spec:review (mode "review")',
  phases: [
    { title: 'Requirements', detail: 'EARS requirements from PLAN.md decisions' },
    { title: 'Design + Test Plan', detail: 'in parallel; the test plan never sees the design' },
    { title: 'Task Tracks', detail: 'implementation and test task lists in parallel' },
    { title: 'Link', detail: 'merge tracks into tasks.md, wire Verified by, lint the dependency graph' },
    { title: 'Adversarial Review', detail: '4 lenses, each finding challenged by a refuter' },
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
const TPL = `${DIR}/test-plan.md`
const REVIEW = `${DIR}/review.md`
const DRAFT_IMPL = `${DIR}/.tasks-impl.draft.md`
const DRAFT_TEST = `${DIR}/.tasks-test.draft.md`

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

  if (!authored.requirements) {
    log('Requirements authoring failed. Stopping.')
    return { mode: MODE, feature: A.feature, specDir: DIR, error: 'authoring failed: requirements' }
  }

  // Design and test plan run in parallel. The test planner works from the
  // requirements and the codebase's real boundaries, never from design.md, so
  // the tests stay an independent oracle instead of mirroring the design.
  phase('Design + Test Plan')
  const [design, testPlan] = await parallel([
    () => agent(`
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

## Public Interfaces
The entry points other code and tests will call: CLI commands, HTTP endpoints, public functions/classes, events, file formats. Exact names and signatures. A separate test plan is being written in parallel against the requirements and these kinds of boundaries, so keep public behavior faithful to the requirements and keep internals behind these interfaces.

### Decision Seams
For every rule that decides whether something is allowed, refused, or held (every SHALL NOT, allowlist, limit, tripwire, or "only"/"never" criterion), expose a pure decision function: no I/O, no clock, no network, all inputs as arguments, e.g. \`decide(inputs) -> Decision\`. Name it, its module, its input and output types, and the criteria it decides. The server, CLI, or handler calls it; it never re-implements the rule. The test plan writes property tests against these seams so the safety rules are checked before any server or HTTP task exists. List each seam's owning component here.

## Data Models
Types and schemas in the project's own language. Every field typed and explained.

## Data Flow
A Mermaid diagram of the main flow (sequence or flowchart).

## Error Handling
One entry per error criterion in requirements.md, referenced by number.

## Security, Performance, Migration
Only the sections that apply.

## Requirements Traceability
| Criterion | Design element |
Every criterion from requirements.md appears here.

Rules:
- Use real paths. Before naming an existing file, confirm it exists. Mark new files as new.
- Match the conventions you actually see in the code (framework, error style, layout).
- No component without a requirement driving it.
- Do not write a testing section; ${TPL} covers testing.

Write the file, then return the structured result.`, { label: 'write design.md', phase: 'Design + Test Plan', schema: WRITE_RESULT }),

    () => agent(`
You are a test architect writing the test plan of a spec. A design is being written in parallel; do NOT read or wait for design.md. Work from the requirements and from how this codebase is actually tested and run, so the tests are an independent check on whatever the implementation turns out to be.

${BRIEF}

${PLAN_RULE}

Read ${REQ}. Then study the codebase's testing reality: test frameworks and runners, existing test directories and fixtures, how the app, CLI, server, database, queues, or external services are started in tests (or could be), CI config, and the commands that run each kind of test.

The most common failure of AI-written tests is a pile of shallow unit tests that mock everything and prove nothing about the system working. Prevent that:
- **Integration first.** Every criterion that involves I/O, persistence, network, configuration, process boundaries, multiple components, or an error that surfaces at a boundary MUST be covered by an integration or end-to-end test that exercises the real boundary: a real database (in-memory engine, temp DB, or container), a real filesystem (temp dirs), a real HTTP server or test client, the real CLI entry point, real subprocesses.
- **Mock only what you don't own** (third-party APIs, payment providers, clocks, randomness). Never mock the project's own modules, database, or filesystem in an integration test.
- **End-to-end smoke tests** for each critical user flow named in the requirements.
- **Unit tests only for pure logic** with meaningful branching or edge cases.
- **Property tests** for every universally quantified criterion (the ## Properties section below), plus invariants (round-trips, idempotence, ordering, conservation). Example tests check the cases someone thought of; properties check the ones nobody did.
- Tests target public behavior: the entry points named in the requirements or that already exist (CLI commands, endpoints, public APIs, files written). They must not depend on private helpers, so they stay valid whatever internal design is chosen.
- Every test must fail before the feature exists, for the right reason (missing behavior), and pass after.

Write ${TPL} with:

# Test Plan: [Feature Name]

## Strategy
Which levels are used and why, given this codebase. State the integration-first rule as applied here.

## Test Infrastructure
What already exists (paths) and what must be added: fixtures, factories, temp DB/containers, server or CLI harness, test data. Concrete enough to implement.

## Test Cases
| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
IDs TC-1, TC-2, ... Every acceptance criterion has at least one case. Error criteria get their own cases.

## Properties   (REQUIRED)
Correctness properties, one row per property:
| ID | Requirement | Property | Generator domain | Expected decision |
P-n | requirement | "for any X ..." | generator domain | expected decision
IDs P-1, P-2, ... Rules:
- **Which criteria.** Every acceptance criterion with a universal quantifier gets at least one property: "any", "every", "all", "only", "never", "no", "each", or SHALL NOT. Read each criterion and ask "for which inputs must this hold?"; if the answer is "all of them in some set", it is a property.
- **Surface forms.** The generator domain enumerates every surface form the input can take, not just the canonical one. For an amount: "$x", "x USD", "USD x", "x.yy", "x,yyy", "x to y", "x-y", "between x and y", "x/unit"; for a range, BOTH ends must be checked. For an email address: case variants, display names ("Name" <addr>), plus-tags, whitespace. For a path: relative, absolute, "..", symlinks. A rule that only checks the first number of "$4.15 to 4.50" passes every example test and fails this property.
- **Negative space.** For every allowlist (only values from a sheet, a config, a set of participants), draw negatives from values that exist in the data source but are not permitted: quantities and SKUs next to prices in the same sheet, the system's own address or the agent's own draft next to real thread participants, IDs of other tenants. Random strings are not negatives; near-misses from the real data are.
- **Complements.** For every "only" or "never" clause, write the complement property too: "only A passes" means "for any non-A, it does not pass", and "never B" means "for any input that produces B, the decision is refuse/hold".
- **Metamorphic relations** where there is no oracle for the exact output: state how the decision must move when the input changes, e.g. "adding an unquoted amount to a passing body can only move pass -> held, never held -> pass", "removing a recipient never turns refuse into pass", "reordering the list does not change the decision".
- **Library and settings.** Name the property-testing library for the project's language (Python: Hypothesis; JS/TS: fast-check; Go: rapid or testing/quick; Rust: proptest; Java: jqwik; Ruby: rantly; Elixir: StreamData) and deterministic settings: derandomized or a fixed seed (Hypothesis: \`@settings(derandomize=True, max_examples=200, database=None)\`; fast-check: \`{ seed: 42, numRuns: 200 }\`), a bounded example count, no example database, so a run is reproducible and the gate never flakes.
- **Decision seam.** Safety properties (SHALL NOT, allowlists, tripwires, limits) target a pure decision function the implementation must expose, e.g. \`decide(inputs) -> decision\`, not the server or HTTP layer, so they run before any server task exists. Name the seam you need (module, function, input and output shape) in the Harness Contract; add one integration test case that shows the boundary actually calls it.
- If the project's language has no property-testing library, each property becomes a table-driven test over the enumerated domain (every surface form and every negative listed in the row).
- If no criterion is universally quantified, write "None" and the reason. Do not drop the section.

## Harness Contract
Interfaces the test harness relies on that the implementation must provide: factories, keyword arguments, entry points, event or file formats, and the decision seams the properties call. Give exact names and signatures, so the implementation tasks can honor them.

## Critical Paths
The end-to-end flows that must work, as numbered steps, each mapped to TC IDs.

## Commands
Exact commands to run each level and each test file (these become task **Verify:** lines).

## Not Tested
What is deliberately left out and why.

Return the structured result after writing the file.`, { label: 'write test-plan.md', phase: 'Design + Test Plan', schema: WRITE_RESULT }),
  ])
  authored.design = design
  authored.testPlan = testPlan

  if (!design || !testPlan) {
    const missing = [!design && 'design', !testPlan && 'test plan'].filter(Boolean).join(', ')
    log(`Authoring failed for: ${missing}. Stopping before task planning.`)
    return { mode: MODE, feature: A.feature, specDir: DIR, error: `authoring failed: ${missing}` }
  }

  const TASK_BLOCK = `
### Task <ID>: [Action-oriented title]
**Status:** Not Started
**Track:** <impl|test>
**Requirements:** 1.1, 1.2
**Description:** What to do, in enough detail that an engineer new to the repo can start without asking.
**Files:**
- \`path/to/file\` - specific change
**Verify:** \`exact command\` \`another command\`
**Properties:** P-1, P-2   (test tasks that write property tests only; omit otherwise)

**Acceptance:**
- [ ] [Concrete check derived from the referenced criteria]

**Dependencies:** None | Task <ID> (reason), Task <ID> (reason)
**Complexity:** Low | Medium | High
`

  phase('Task Tracks')
  const [implTasks, testTasks] = await parallel([
    () => agent(`
You are writing the IMPLEMENTATION track of a spec's task list. Another agent is writing the test track in parallel from ${TPL}; tests are not your job.

${BRIEF}

Read ${REQ} and ${DES}. Write a draft to ${DRAFT_IMPL}: a list of task blocks in exactly this shape, with provisional IDs I1, I2, ...:
${TASK_BLOCK}

Rules:
- **Track:** impl on every task.
- Every acceptance criterion in requirements.md is implemented by at least one task (list it under **Requirements:**).
- **Files:** lists only source files (and unit tests for pure helpers you introduce). Never list files under the project's test directories that the test plan owns.
- **Verify:** the command(s) that prove this task works, e.g. the relevant test files from ${TPL}'s Commands section. Leave it empty if only the project-wide suite applies.
- Small tasks, one focused change set each. Two tasks that could run in parallel must not edit the same file.
- Add a dependency only when this task calls code, reads data, or edits a file that the other task creates. Every dependency carries a one-line reason in parentheses, e.g. "Task I3 (calls parse_config)". Every dependency serializes the run, so don't add order-only edges.
- No task references a component absent from design.md.

Write the draft file, then return the structured result.`, { label: 'write implementation track', phase: 'Task Tracks', schema: WRITE_RESULT }),

    () => agent(`
You are writing the TEST track of a spec's task list. Another agent is writing the implementation track in parallel.

${BRIEF}

Read ${REQ}, ${TPL}, and ${DES} (use design.md only for the names of public interfaces the tests call). Write a draft to ${DRAFT_TEST}: task blocks in exactly this shape, with provisional IDs T1, T2, ...:
${TASK_BLOCK}

Rules:
- **Track:** test on every task.
- If ${TPL} needs new test infrastructure, T1 builds it (fixtures, harness, temp DB/container setup) and other test tasks depend on it.
- Group test cases by boundary or user flow, one task per group. In **Description**, list the TC IDs it implements; together the tasks cover every TC in ${TPL}.
- **Requirements:** the criteria those test cases cover. This is how tasks are linked to the implementation that must make them pass.
- **Files:** the test files and fixtures the task creates. These files are owned by the test track; implementation tasks may not weaken them.
- **Verify:** the exact command that runs this task's tests (from ${TPL} Commands).
- Acceptance must include: tests exercise the real boundary named in the test plan; tests fail before the implementation exists for the right reason (missing behavior, not broken test code); no mocks of the project's own code in integration tests.
- Test tasks depend only on other test tasks (infrastructure), never on implementation tasks, so they can start as soon as the spec is approved.
- **Properties.** Write one test task per property group from ${TPL}'s ## Properties section (properties that share a decision seam or a requirement story). List its P-ids under **Properties:** (e.g. \`**Properties:** P-1, P-2\`) and in the Description; together the tasks cover every P-n. **Verify:** runs that task's property test file. The test file declares a module-level map from each property to the test that checks it, in the file's own language: \`PROPERTIES = {"P-3": "test_name", "P-4": ["test_a", "test_b"]}\` (JS/TS: \`export const PROPERTIES = { 'P-3': 'test name' }\`). \`ck gate\` reads these maps to check every P-n is covered. Acceptance includes: the generators cover every surface form and negative listed in the row; settings are deterministic; the tests call the decision seam, not a server.
- **Red-team tasks.** For each user story with a SHALL NOT criterion, add a red-team test task. Like every test task it is written before the code and from the requirements only, never from the implementation. Its Description: "Try to break each SHALL NOT clause of Story N with adversarial inputs: unusual surface forms, near-miss values from the real data, the system's own outputs fed back as inputs, boundary and encoding tricks. Every way you find to break it becomes a test." Its **Requirements:** are the story's SHALL NOT criteria; it verifies the impl tasks that implement them.

Write the draft file, then return the structured result.`, { label: 'write test track', phase: 'Task Tracks', schema: WRITE_RESULT }),
  ])
  authored.implTasks = implTasks
  authored.testTasks = testTasks

  if (!implTasks || !testTasks) {
    const missing = [!implTasks && 'implementation track', !testTasks && 'test track'].filter(Boolean).join(', ')
    log(`Authoring failed for: ${missing}. Stopping before linking.`)
    return { mode: MODE, feature: A.feature, specDir: DIR, error: `authoring failed: ${missing}` }
  }

  phase('Link')
  authored.tasks = await agent(`
You are merging two task tracks into the final task list of a spec. Tooling parses the result, so follow the format exactly.

${BRIEF}

Inputs: ${DRAFT_IMPL} (implementation track, IDs I1..), ${DRAFT_TEST} (test track, IDs T1..). Also read ${REQ}, ${DES}, and ${TPL}.

1. Renumber all tasks as integers: test-infrastructure task first, then interleave so the order reads as a sensible plan. Rewrite every **Dependencies:** reference to the new numbers.
2. Link the tracks by requirement overlap:
   - On each impl task add \`**Verified by:** Task N, Task M\`: the test tasks whose **Requirements:** overlap its own.
   - On each test task add \`**Verifies:** Task N, Task M\`: the impl tasks it verifies.
   - Add each impl task's verifying test tasks to its **Dependencies:** with the reason "(its tests are the oracle)". Tests are written first.
   - **Verify-order rule:** a test task may only verify an impl task whose code the tests can run without help from later tasks. If test task T exercises a path through code owned by impl task Y (for example a runner or CLI entry point), and Y depends on impl task X, then T must verify Y, not X. X becomes a foundation task whose own **Verify:** is a smoke command, and its criteria are verified at Y. Otherwise X's gate can never pass.
   - Every impl task that implements a requirement criterion has at least one verifying test task; every test task verifies at least one impl task. If a gap exists, add the missing task, consistent with ${TPL}.
   - Give every interface in ${TPL}'s Harness Contract an owning impl task, and note it in that task's Description. Each decision seam gets its own small impl task (or is owned by the first task that needs it) with no dependency on server, HTTP, or CLI tasks.
   - Property test tasks verify the impl task that owns their decision seam, not the server or handler built on it, so every P-n runs as soon as the seam exists. Keep each test task's **Properties:** field. Check that every P-n in ${TPL}'s ## Properties section is listed under **Properties:** by exactly one test task; add a test task for any that is not.
   - Red-team test tasks verify the impl tasks that implement their story's SHALL NOT criteria.
   - Reference fields (**Dependencies:**, **Verified by:**, **Verifies:**) hold task references with their reasons in parentheses, nothing else. Put explanatory notes on their own line: tooling reads every "Task N" in those fields.
   - Every dependency keeps a one-line reason in parentheses. Drop any dependency that has no code, data, test, or shared-file reason.
3. Write ${TSK}:

# Implementation Tasks: [Feature Name]

**Status:** Not Started
**Spec:** [requirements.md](requirements.md) · [design.md](design.md) · [test-plan.md](test-plan.md)${A.planPath ? ' · [PLAN.md](PLAN.md)' : ''}

## Task Breakdown

(all task blocks: "### Task N: Title" headers, "- \`path\` - note" file lines under **Files:**, fields **Status:** **Track:** **Requirements:** **Description:** **Files:** **Verify:** **Properties:** (property test tasks) **Verified by:**/**Verifies:** **Acceptance:** **Dependencies:** **Complexity:**, separated by ---)

## Dependency Graph
A Mermaid graph TD of task dependencies, with test tasks visually distinct (e.g. a "test" class).

## Schedule
There are no waves: each task starts as soon as its own Dependencies are merged, so the plan is the dependency graph above. Two lines, copied from \`ck lint\` in step 5:
- **Ready at start:** Tasks …
- **Critical path:** Task A -> Task B -> … (N of M tasks; the floor on wall-clock time however many agents run)

4. Delete ${DRAFT_IMPL} and ${DRAFT_TEST}.
5. Run \`ck lint ${A.feature}\` from ${ROOT}. Fix every dependency cycle and every verify-order cycle it reports, and add a reason to every dependency it lists as reasonless (or remove the dependency). Re-run it until it reports no cycles. Every edge on the critical path lengthens the run: drop any that has no code, data, test, or shared-file reason. Then fill the Schedule section from its "critical path" and "ready now" lines.

Return the structured result.`, { label: 'link tracks into tasks.md', phase: 'Link', schema: WRITE_RESULT })

  if (!authored.tasks) {
    log('Linking failed. Drafts are left in the spec directory. Stopping before review.')
    return { mode: MODE, feature: A.feature, specDir: DIR, error: 'linking task tracks failed' }
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
          file: { type: 'string', enum: ['requirements.md', 'design.md', 'test-plan.md', 'tasks.md', 'PLAN.md'] },
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

const SPEC_FILES = `${REQ}, ${DES}, ${TPL}, ${TSK}${A.planPath ? `, decision record ${A.planPath}` : ''}`

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
- Are dependencies correct and acyclic? Do two tasks that can run at the same time (neither depends on the other, directly or transitively) edit the same file? They will conflict at merge: flag it, and propose splitting the file's ownership or a dependency with the reason "shared file".
- Does each task name its files and a **Verify:** command, and is it small enough to finish in one sitting?
- Is every impl task linked to the test tasks that verify it (**Verified by:**), with those test tasks in its Dependencies? Do test tasks depend only on test infrastructure, never on impl tasks?
- Verify-order: can each impl task's verifying tests pass when only that task and its dependencies exist? A test that drives a runner or entry point built by a LATER task (one that depends on this task) makes this task's gate unpassable. Flag it, with the fix: make this task a foundation task, and have the test task verify the later task.
- Dependencies: does each dependency have a reason (calls its code, reads its data, its tests are the oracle, a shared file)? Every dependency serializes the run, so flag edges with no such reason and propose removing them. Run \`ck lint ${A.feature}\` from ${ROOT} and include what it reports.
- Does any impl task list a test file owned by a test task (the guard will block it from weakening those)?
- Format: "### Task N: Title" headers, "- \`path\` - note" file lines, and **Status:** / **Track:** / **Verify:** / **Verified by:** / **Verifies:** fields (tooling parses these).
- Pretend you are implementing Task 1 and the riskiest task right now. Where would you get stuck or have to guess?`,
  },
  {
    key: 'verification',
    prefix: 'V',
    focus: `TESTS THAT WOULD ACTUALLY CATCH A BROKEN FEATURE.
- ${TPL} exists? If not, that is a critical finding: propose the test plan's outline as the fix.
- For each criterion involving I/O, persistence, network, config, process boundaries, multiple components, or boundary errors: is there an integration or end-to-end test that exercises the REAL boundary (real DB/temp DB, temp filesystem, real HTTP/test client, real CLI entry point)? Flag criteria covered only by unit tests or mocks.
- Flag any plan to mock the project's own modules, database, or filesystem in an integration test.
- Do the critical user flows have end-to-end smoke tests? Are error criteria tested at the boundary where the user sees them?
- Do tests target public interfaces (CLI, endpoints, public APIs, written files) rather than private helpers, so they survive refactoring? Check names against design.md's Public Interfaces and the existing code.
- Is the test infrastructure (fixtures, harness, containers) concrete enough to build, and does a test task build it first?
- Would each test fail before the feature exists for the right reason? Are the **Verify:** commands real, runnable commands for this repo?
- Imagine a plausible implementation bug for the riskiest criterion. Which test catches it? If none, that is a finding.
- Properties: does ${TPL} have a ## Properties section? Does every universally quantified SHALL / SHALL NOT criterion ("any", "every", "all", "only", "never", SHALL NOT) have a P-n row? A missing one is a major finding.
- For each property: does the generator domain enumerate the input's surface forms (e.g. "$x", "x USD", "x to y", "x-y" for amounts, checking both ends of a range)? For each allowlist, does it draw from the negative space: values that exist in the data source but are not permitted (quantities next to prices, the system's own draft next to participants)? Does each "only"/"never" clause have its complement property? Where there is no oracle, is there a metamorphic relation?
- Do safety properties target a pure decision seam that runs before any server task, with deterministic settings (derandomized, bounded examples, no example database)? Does every P-n belong to exactly one test task's **Properties:** field, whose test file will carry a PROPERTIES map? Does each story with a SHALL NOT have a red-team test task?`,
  },
]

phase('Adversarial Review')
const reviewed = await pipeline(
  LENSES,
  lens => agent(`
You are an adversarial reviewer of a spec. Assume it is wrong until the files prove otherwise. Your job is to find concrete defects that would cause failed implementation, rework, or a feature that does not do what was decided.

${BRIEF}

Spec files: ${SPEC_FILES}.

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

Spec files: ${SPEC_FILES}.

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

Spec files: ${SPEC_FILES}.

1. Apply each of these confirmed findings by editing the spec files. Keep the documents consistent with each other (if you change a criterion, update design traceability, the test plan's cases and properties, and the covering impl and test tasks). Keep the tasks.md format intact ("### Task N: Title", "- \`path\` - note").
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

## Schedule
Re-run \`ck lint ${A.feature}\` from ${ROOT} after your fixes. Update the Schedule section of tasks.md from its "critical path" and "ready now" lines, and copy those two lines here.

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
      schedule: { type: 'string', description: 'the ready-at-start and critical-path lines' },
    },
    required: ['fixed', 'notFixed', 'result', 'schedule'],
  },
})

return {
  mode: MODE,
  feature: A.feature,
  specDir: DIR,
  files: MODE === 'create' ? [REQ, DES, TPL, TSK, REVIEW] : [REVIEW],
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
  schedule: revision ? revision.schedule : null,
}
