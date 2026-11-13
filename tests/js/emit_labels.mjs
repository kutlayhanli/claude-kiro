// Prints every agent label the two real workflow scripts emit, as JSON on stdout:
// [{workflow, label, phase}]. Run: node tests/js/emit_labels.mjs
// The scripts run for real through load_workflow.mjs; only `agent` is stubbed, by the small
// scenario-driven fake below. The scenarios cover every label template family of the
// roles grammar (design.md S5): retry, escalation, review rounds, conflict rounds, landing
// fix, full and final gate, re-check, cross-test, plan retry, wave-batched review,
// report, every spec-create phase, and (run-principles) the arbiter, the auditor, their
// `arbiter-` thin steps, the create-side arbiter steps and the `apply-cuts` mode.
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, labelCorpus } from './load_workflow.mjs'
import { arbiterArgv, arbiterStdin, isCkCall, THIN_LABEL, gateJson } from './fake_project.mjs'

const GATE_LABEL = /^(full gate @|re-check |final gate$|gate$)/

const here = path.dirname(fileURLToPath(import.meta.url))
const IMPLEMENT = path.join(here, '../../plugin/workflows/spec-implement.js')
const CREATE = path.join(here, '../../plugin/workflows/spec-create.js')

const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
const AGENTS = {
  implementer: { model: 'haiku', effort: 'low', escalate: ['sonnet', 'opus'] },
  test_writer: { model: 'haiku', effort: 'low' },
  fixer: { model: 'haiku', effort: 'low', escalate: ['sonnet', 'opus'] },
  reviewer: { model: 'sonnet', effort: 'high', enabled: true, rounds: 2, cross_test: true },
  orchestrator: { model: 'sonnet' },
  resolver: { model: 'sonnet' },
}
// run-principles: the resolved arbiter role and the agent type names the skills pass (design.md "Workflow args").
const ARBITER_ROLE = { model: 'opus', effort: 'high', risky_model: 'opus', risky_effort: 'max' }
const AGENT_TYPES = { arbiterAgent: 'spec:arbiter', auditorAgent: 'spec:auditor', runnerAgent: 'spec:runner' }
const PRINCIPLES = [{ id: 'P1', layer: 'project', text: 'Tests verify the requirements, not the code.' }]

const words = prompt => arbiterArgv(prompt) || []
const after = (argv, w) => argv[argv.indexOf(w) + 1]
const stdinJson = prompt => { try { return JSON.parse(arbiterStdin(prompt) || 'null') } catch { return null } }
const envelope = json => ({ exitCode: 0, json, error: null })

// The `arbiter-` thin steps of spec-implement (design.md "Workflow schemas"): an OpenResult that rules, a SettleResult
// that asks for one re-run at the risky tier on a claim's first ruling (so `arbiter task <n> risky` runs) and then
// routes as `routes[n]` says (default `code`), and the record's {number, commit}.
function arbiterSteps({ routes = {} }) {
  let opened = 0
  let recorded = 0
  const settles = {}
  const taskOf = {}
  return (l, prompt) => {
    const argv = words(prompt)
    const cmd = argv[0] === '-C' ? argv.slice(2) : argv
    const step = l.startsWith('arbiter-') ? l.slice('arbiter-'.length).split(' ')[0] : cmd[1]
    if (step === 'open') {
      const k = ++opened
      const input = stdinJson(prompt) || {}
      const n = String(after(argv, '--task') ?? input.claim?.task ?? '1')
      const key = input.claim?.key || `${n}|claim:${k}`
      taskOf[key] = n
      return envelope({
        action: 'rule', key, tier: 'normal', reason: '', principles: PRINCIPLES, planLayer: 'present',
        worktree: `/repo/.ck/worktrees/demo-task-arbiter${k}`, branch: `feat/demo-task-arbiter${k}`, number: String(k),
        earlier: [], missing: null,
      })
    }
    if (step === 'settle') {
      const key = cmd[3] ?? 'key'
      const c = settles[key] = (settles[key] || 0) + 1
      const route = c === 1 ? 'rerun' : (routes[taskOf[key]] || 'code')
      return envelope({ route, reason: route === 'rerun' ? 'the ruling asked for a stronger look' : '', record: route !== 'rerun', principle: route === 'rerun' ? null : PRINCIPLES[0], problems: [] })
    }
    if (step === 'record') { recorded++; return envelope({ number: recorded, commit: `d${recorded}` }) }
    return null
  }
}

// A scenario fake: knobs drive which labels the script reaches.
// run-principles knobs: failUntilRuled / guardUntilRuled / conflictUntilRuled (task numbers whose attempts fail at
// the gate, stop on the guard, or report a test that can never pass until `arbiter task <n>` has answered: a
// `gate-failure` past retries and escalation, a `guard-stop` or a `test-conflict` claim), verifiedBy (the plan's
// field, so `auditDue` holds for an impl task whose test task no cross-test passed), routes ({n: SettleResult route}).
function fake({ deps, failUntil = {}, reviews = {}, conflictRounds = {}, redLands = {}, recheckRed = 0, fullRed = false, planFailFirst = false,
  failUntilRuled = [], guardUntilRuled = [], conflictUntilRuled = [], verifiedBy, routes = {}, unreproduced = null }) {
  const done = new Set()
  const attempts = {}
  const reviewCount = {}
  const hit = {}
  const red = { ...redLands }
  const ruled = {}
  const thin = arbiterSteps({ routes })
  let recheckLeft = recheckRed
  let planCalls = 0
  const raw = async (prompt, opts) => {
    const l = opts.label
    if (l.startsWith('arbiter-') || (isCkCall(opts) && /^(arbiter|principles)$/.test(words(prompt)[words(prompt)[0] === '-C' ? 2 : 0] || ''))) {
      const a = thin(l, prompt)
      if (a) return a
    }
    if (isCkCall(opts) && words(prompt).length === 0 && /\bck .*\blint\b.*--simulate/.test(prompt)) {
      const n = (prompt.match(/--task (\d+)/) || [])[1] || '1'
      return envelope({ task: n, suites: [{ file: 'tests/test_demo.py', command: 'pytest' }], selected: [{ file: 'tests/test_demo.py', tests: null }] })
    }
    if (l.startsWith('plan')) {
      planCalls++
      if (planFailFirst && planCalls === 1) return null
      const ready = Object.keys(deps).filter(n => !done.has(n) && deps[n].every(d => done.has(d)))
      const exclude = ((prompt.match(/--exclude ([\d,]+)/) || [])[1] || '').split(',').filter(Boolean)
      return { done: [...done], deps, ready: ready.filter(n => !exclude.includes(n)), busy: [], remaining: Object.keys(deps).filter(n => !done.has(n)), cycles: [], verifyCycles: [], ...(verifiedBy ? { verifiedBy, verifyScope: [] } : {}) }
    }
    if (l.startsWith('task')) {
      const n = l.match(/task (\d+)/)[1]
      attempts[n] = (attempts[n] || 0) + 1
      const held = list => list.map(String).includes(n) && !ruled[n]
      const pass = attempts[n] > (failUntil[n] || 0) && !held(failUntilRuled) && !held(guardUntilRuled) && !held(conflictUntilRuled)
      const extra = pass ? {}
        : held(guardUntilRuled) ? { guardStop: { file: `tests/test_t${n}.py`, kind: 'test', test: `test_tc${n}_x`, requirement: '1.1' } }
          : held(conflictUntilRuled) ? { testConflict: { file: `tests/test_t${n}.py`, test: `test_tc${n}_x`, criterion: '1.1', reason: 'asserts the opposite of 1.1', fix: 'assert what 1.1 says' } }
            : {}
      return { task: n, status: pass ? 'done' : 'in_progress', gatePassed: pass, summary: `attempt ${attempts[n]}`, commits: 'abc123', startedAt: '2026-10-04T10:00:00Z', finishedAt: '2026-10-04T10:01:00Z', ...extra }
    }
    if (l.startsWith('land ')) {
      const out = []
      for (const n of l.slice(5).split(' ')) {
        if (n.startsWith('arbiter')) { out.push({ task: n, state: 'MERGED', checkGreen: true, commit: `c${n}`, moved: [], mergedAt: '2026-10-04T10:02:00Z' }); continue }
        if ((hit[n] || 0) < (conflictRounds[n] || 0)) { hit[n] = (hit[n] || 0) + 1; out.push({ task: n, state: 'CONFLICT', files: ['specs/demo/tasks.md'] }); continue }
        if ((red[n] || 0) > 0) { red[n]--; out.push({ task: n, state: 'RED', checkGreen: false, checkTail: `red: task ${n}` }); continue }
        done.add(n)
        out.push({ task: n, state: 'MERGED', checkGreen: true, mergedAt: '2026-10-04T10:02:00Z' })
      }
      const batch = l.slice(5).split(' ')
      if (unreproduced && !hit.unreproduced && unreproduced.batch.every(n => batch.includes(n)) && out.every(t => t.state === 'MERGED')) {
        // run-principles Story 15: red as a batch, no task reproduces it: the run sends it to the full gate
        hit.unreproduced = 1
        return { tasks: out, gates: [{ tasks: batch, green: false }, ...batch.map(n => ({ tasks: [n], green: true }))], bisected: true, unreproduced: { tasks: batch, tail: 'red as a batch', failures: [] }, startedAt: '2026-10-04T10:01:00Z', finishedAt: '2026-10-04T10:02:00Z' }
      }
      return { tasks: out, gates: [{ tasks: [], green: true }], bisected: false, startedAt: '2026-10-04T10:01:00Z', finishedAt: '2026-10-04T10:02:00Z' }
    }
    if (l.startsWith('resolve')) return { resolved: true, conflicts: 'kept both' }
    if (l.startsWith('review')) {
      // A review-wave label covers a merged wave: advance the count for every
      // covered task, look the verdict up by the first covered task's entry,
      // and return a verdict shaped like an individual review.
      const w = l.match(/^review-wave (\d+) tasks ([\d ]+)$/)
      const covered = w ? w[2].split(' ') : null
      const n = covered ? covered[0] : l.match(/review (\d+)/)[1]
      for (const t of (covered || [n])) reviewCount[t] = (reviewCount[t] || 0) + 1
      const verdict = (reviews[n] || [])[reviewCount[n] - 1] || 'approve'
      return { verdict, findings: verdict === 'changes' ? [{ severity: 'blocking', file: 'src/demo.py', issue: 'contradicts design.md' }] : [], summary: verdict }
    }
    if (l.startsWith('cross-test')) return { verdict: 'approve', findings: [], summary: 'consistent' }
    if (l === 'report') return { head: 'f00d', merged: [...done].map(n => ({ task: n, commit: `m${n}`, mergedAt: '2026-10-04T11:00:00Z' })), done: [], failed: [], blocked: [], unstarted: [] }
    // The arbiter's RULING (design.md "Workflow schemas"): a first call asks for a stronger look, so the risky
    // re-run is reached; the risky call rules, and the task's held attempts may then pass.
    if (l.startsWith('arbiter task')) {
      const n = l.match(/^arbiter task (\d+)/)[1]
      const claim = (prompt.match(/\b(\d+\|[\w:.-]+)/) || [])[1] || `${n}|claim`
      if (!/ risky$/.test(l)) return { claim, status: 'needs_stronger_look', reasoning: 'the spec and the test disagree; a stronger look is needed', established: 'which criterion the claim names' }
      ruled[n] = true
      const route = routes[n] || 'code'
      return { claim, status: 'ruled', outcome: route, reasoning: 'P1 decides it', principle: 'P1', ...(route === 'code' ? { code: 'make the code meet 1.1' } : route === 'spec_edit' ? { changed: 'design.md now says what 1.1 says', commits: 'e1' } : {}) }
    }
    // The auditor's AUDIT: no problems, so the task starts.
    if (l.startsWith('audit task')) return { problems: [], summary: 'the selected tests read the spec correctly' }
    if (l.startsWith('fix')) return { green: true }
    if (l.startsWith('re-check')) return { green: recheckLeft-- <= 0 }
    if (l.startsWith('full gate')) return { green: !fullRed }
    if (l === 'final gate') return { green: true }
    if (l === 'run start') return { integration: { branch: 'integrate/demo', state: 'ok' }, hooks: { installed: true, active: true, intact: true, chainedTo: null, problems: [] }, baseline: { commit: 'abc1234', files: 0 }, releasedClaims: [], keptClaims: [], warnings: [] }
    if (l === 'audit') return { schema: 'ck-audit/1', spec: 'demo', into: 'integrate/demo', head: 'abc1234', complete: true, clean: true, drift: [], hooks: { installed: true, active: true, intact: true, chainedTo: null, problems: [] }, fixtures: { drift: [], newLocks: [], lockTampered: false, handcrafted: [] }, problems: [] }
    if (l.startsWith('park ')) return { parked: true, notification: 'sent', detail: '' }
    if (l.startsWith('relink ')) return { relinked: true }
    // today's test-fix path for a test conflict (the arbiter replaces its entry point, Task 65)
    const nodes = () => [...String(prompt).matchAll(/--test[ =](\S+)/g)].map(m => m[1].replace(/^["']|["']$/g, ''))
    if (l.startsWith('testfix ledger ')) return { allowed: nodes(), alreadyFixed: [] }
    if (l.startsWith('testfix prove ')) return { scope: { in_scope: true, outside: [], requirements_edited: false, spec_docs_edited: [], reason: 'in scope' }, proof: { proved: true, against: 'integration', outcomes: nodes().map(node => ({ node, passed: true, cls: null })), reason: 'proved' }, changedTests: nodes(), against: 'integration' }
    if (l.startsWith('testfix review ')) return { verdict: 'approve', findings: [], summary: 'approve' }
    if (l.startsWith('testfix record ')) return { recorded: true }
    if (l.startsWith('testfix ')) return { verdict: 'fixed', changedTests: [{ file: 'tests/test_t3.py', test: 'test_tc3_x' }], commits: 'fix123', finding: null, summary: 'fixed' }
    throw new Error('unexpected agent ' + l)
  }
  // Thin `ck` steps (thinCk on master): a call carrying the CK_CALL schema or an agentType gets the CK_CALL envelope
  // {exitCode, json}; a gate step's json is a ck-gate/1 object, as fake_project.mjs answers them.
  return async (prompt, opts) => {
    const l = opts.label
    if (l.startsWith('arbiter-') || !(THIN_LABEL.test(l) && isCkCall(opts))) return raw(prompt, opts)
    const out = await raw(prompt, opts)
    if (GATE_LABEL.test(l)) return { exitCode: out.green ? 0 : 1, json: gateJson(out.green) }
    if (out && typeof out === 'object' && 'exitCode' in out && 'json' in out) return out
    return out == null ? null : envelope(out)
  }
}

const scenarios = [
  // retry (task 1), escalation (task 2 climbs sonnet then opus), [test] tasks, review rounds with escalated revise
  { deps: { 1: [], 2: [], 3: [], 4: [] }, tracks: { 3: 'test', 4: 'test' }, failUntil: { 1: 1, 2: 3 }, reviews: { 3: ['changes', 'approve'], 4: ['changes', 'changes', 'changes'] } },
  // conflict rounds (resolve, resolve round 2), landing fix with escalated fixers, plan retry, cross-test
  { deps: { 1: [], 2: ['1'] }, tracks: { 1: 'test', 2: 'test' }, conflictRounds: { 1: 2 }, redLands: { 2: 2 }, planFailFirst: true },
  // full gate after every merge, red once so fix + re-check (and escalated fix + re-check) run
  { deps: { 1: [], 2: ['1'] }, fullGateEvery: 1, fullRed: true, recheckRed: 1 },
  // a batch landing several tasks at once, final gate and report
  { deps: { 1: [], 2: [], 3: [] }, mergeBatch: 3, fullGateEvery: 0 },
  // a merged wave of 4 non-risky tasks reviewed together in one review-wave agent
  { deps: { 1: [], 2: [], 3: [], 4: [] }, mergeBatch: 4, fullGateEvery: 0 },
  // run-principles: every claim trigger reaches the arbiter (design.md claimTrigger): task 1 fails its gate past
  // retries and escalation (gate-failure), task 2 stops on the guard (guard-stop) and is ruled a spec edit, so the
  // arbiter's own landing runs, task 3 reports a test that can never pass (test-conflict), task 4's review deadlocks
  // (review-deadlock). Each first ruling asks for a stronger look, so `arbiter task <n> risky` runs. verifiedBy names
  // test task 5, which no cross-test passes (cross_test is off), so `audit task <n>` runs for the impl tasks.
  {
    deps: { 1: [], 2: [], 3: [], 4: [], 5: [] }, tracks: { 3: 'test', 5: 'test' }, arbiter: true, crossTest: false,
    failUntilRuled: [1], guardUntilRuled: [2], conflictUntilRuled: [3], reviews: { 4: ['changes', 'changes', 'changes', 'approve'] },
    verifiedBy: { 1: ['5'], 2: ['5'], 4: ['5'] }, routes: { 2: 'spec_edit' },
  },
  // run-principles Story 15: a landing red as a batch only, no task RED: `full gate @1 (unreproduced landing)`
  { deps: { 1: [] }, mergeBatch: 1, fullGateEvery: 0, unreproduced: { batch: ['1'] } },
]

for (const { tracks, fullGateEvery, mergeBatch, arbiter, crossTest, ...knobs } of scenarios) {
  const agents = arbiter ? { ...AGENTS, reviewer: { ...AGENTS.reviewer, cross_test: crossTest !== false }, arbiter: ARBITER_ROLE } : AGENTS
  const args = {
    ...baseArgs,
    agents,
    ...(arbiter ? { arbiterAgent: AGENT_TYPES.arbiterAgent, auditorAgent: AGENT_TYPES.auditorAgent, verifiedBy: knobs.verifiedBy } : {}),
    tracks: tracks || {},
    wave: Object.fromEntries(Object.keys(knobs.deps).map(n => [n, 1])),
    maxRetries: 1,
    ...(fullGateEvery !== undefined ? { fullGateEvery } : {}),
    ...(mergeBatch ? { mergeBatch } : {}),
  }
  await runWorkflow(IMPLEMENT, { agent: fake(knobs), args })
}

// spec-create: every phase (authoring, adversarial review, snapshot, revise, consistency, cleanup)
const createArgs = { mode: 'create', feature: 'f', projectRoot: '/p', specDir: '/p/specs/f', date: '2026-10-04' }
// run-principles (design.md "/spec:create"): `refute: fidelity` refutes R2 (so `arbiter refuted` has a subject),
// `review: should-exist` proposes cut C1 (so `arbiter cuts` has one), and `ck arbiter context` reports a PLAN.md
// layer (so `arbiter drift` runs). A first cuts ruling asks for a stronger look, so `arbiter cuts risky` runs.
// The thin `ck arbiter context|lane|review` and `ck lint` steps answer in the CK_CALL envelope.
function createAgent({ cut = 'apply' } = {}) {
  return async (prompt, opts) => {
    const l = opts.label
    if (isCkCall(opts)) {
      const argv = words(prompt)
      const cmd = argv[0] === '-C' ? argv.slice(2) : argv
      if (cmd[0] === 'arbiter' && cmd[1] === 'context') return envelope({ principles: PRINCIPLES, planLayer: 'present', implementationStarted: cut === 'frozen', missing: null, cut })
      if (cmd[0] === 'arbiter' && cmd[1] === 'lane') return envelope({ ok: true, lane: cmd[2] })
      if (cmd[0] === 'arbiter' && cmd[1] === 'review') {
        const input = stdinJson(prompt) || {}
        const results = (input.subjects || []).map((s, i) => ({ id: (s && s.id) || String(i + 1), action: 'recorded', reason: '', stronger: false, ruling: i + 1 }))
        return envelope({ results, lost: false })
      }
      if (/\bck .*\blint\b/.test(prompt)) return envelope({ ok: true, failures: [], warnings: [] })
      if (/\bck .*\bprogress\b/.test(prompt)) return envelope({ ok: true })
    }
    if (l.startsWith('write') || l.startsWith('link')) return { path: 'x', summary: 's', assumptions: [], concerns: [] }
    if (l === 'review: fidelity') return { findings: [
      { id: 'R1', file: 'requirements.md', location: '1', severity: 'major', problem: 'p', evidence: 'e', fix: 'f', needsUserDecision: false },
      { id: 'R2', file: 'requirements.md', location: '2', severity: 'minor', problem: 'p2', evidence: 'e', fix: 'f', needsUserDecision: false }] }
    if (l === 'refute: fidelity') return { verdicts: [{ id: 'R1', refuted: false, reason: 'real' }, { id: 'R2', refuted: true, reason: 'style only' }] }
    if (l === 'review: should-exist') return { findings: [{ id: 'C1', file: 'requirements.md', location: 'Story 3', severity: 'minor', problem: 'Story 3 serves no goal of PLAN.md', evidence: 'e', fix: 'cut Story 3', needsUserDecision: false, story: '3', title: 'Export', weakness: 'no plan decision needs it', saves: { criteria: 2, tasks: 1, testCases: 2 } }] }
    if (l.startsWith('review:')) return { findings: [] }
    if (/^arbiter refuted/.test(l)) return { rulings: [{ claim: 'R2', status: 'ruled', outcome: 'spec_edit', verdict: 'refutation stands', reasoning: 'P1: wording only', principle: 'P1' }] }
    if (l === 'arbiter cuts') return { rulings: [{ claim: 'C1', status: 'needs_stronger_look', reasoning: 'the cut may drop a decided goal', established: 'Story 3 is in PLAN.md scope' }] }
    if (l === 'arbiter cuts risky') return { rulings: [{ claim: 'C1', status: 'ruled', outcome: 'spec_edit', verdict: 'cut is sound', reasoning: 'P1', principle: 'P1' }] }
    if (/^arbiter drift/.test(l)) return { rulings: [{ claim: 'drift', status: 'ruled', outcome: 'spec_edit', verdict: 'no drift', reasoning: 'the spec follows PLAN.md', principle: 'P1' }] }
    if (l.startsWith('apply-cuts')) return { applied: ['C1'], summary: 'removed Story 3 from requirements, design, test plan and tasks' }
    if (l.startsWith('revise')) return { fixed: ['R1'], notFixed: [], escalated: [], result: 'Ready', schedule: 'Ready at start: Task 1' }
    if (l === 'snapshot spec before revision') return { copied: ['requirements.md'] }
    if (l.startsWith('consistency check')) return { contradictions: [] }
    if (l === 'remove pre-revision snapshot') return { removed: true }
    throw new Error('unexpected ' + l)
  }
}
await runWorkflow(CREATE, { agent: createAgent(), args: createArgs })
const createArbiter = { arbiterAgent: AGENT_TYPES.arbiterAgent, runnerAgent: AGENT_TYPES.runnerAgent, agents: { arbiter: ARBITER_ROLE } }
await runWorkflow(CREATE, { agent: createAgent(), args: { ...createArgs, ...createArbiter } })
// the `apply-cuts` mode the create skill launches for an approved cut (cut `apply`: one `apply-cuts` agent, then lint)
await runWorkflow(CREATE, { agent: createAgent({ cut: 'apply' }), args: { ...createArgs, ...createArbiter, mode: 'apply-cuts', cuts: [{ id: 'C1', story: '3', title: 'Export' }] } })

process.stdout.write(JSON.stringify(labelCorpus, null, 1) + '\n')
