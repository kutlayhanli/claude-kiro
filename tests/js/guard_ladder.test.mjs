// Guard stops under run-principles (6.2, 6.10): a guard stop goes to the arbiter and never climbs the guard ladder;
// an arbiter that cannot be called or returns nothing valid gives a needs-input entry, still never the ladder. Migrated
// from TC-29..TC-35, TC-79 and the Workflow halves of TC-36 and TC-80 (the guard-escalation ladder, parking and the
// decision note), run against the real plugin/workflows/spec-implement.js under the fake runtime. The arbiter steps
// (`arbiter-open`, `arbiter-settle`, `arbiter-record`) run the real `ck` CLI on a temporary git project holding
// specs/PRINCIPLES.md and the spec, so a scenario cannot skip the real arbiter. Ordinary gate failures keep their
// retries and escalation (TC-32); parking, the notification route and the decision note keep their assertions.
// Run: node tests/js/guard_ladder.test.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, useVirtualTime } from './load_workflow.mjs'
import { project } from './fake_project.mjs'
import { VirtualClock } from './virtual_clock.mjs'

useVirtualTime()

const here = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.join(here, '../..')
const SCRIPT = path.join(REPO, 'plugin/workflows/spec-implement.js')

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

// --- a real git project for the arbiter steps ---------------------------------------------------------------------
const PRINCIPLES = `# Principles

## Principles

- **test-intent:** a test states what the requirement says; repair the test, never bend the requirement to it.
- **remove-first:** remove before you optimize.
- **ask-on-reserved:** a reserved scope is decided by me, not by the arbiter.
`
const taskBlock = (n, deps) => `### Task ${n}: Work ${n}
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** Do work ${n}.
**Files:**
- \`src/work${n}.py\` - the work
**Verify:** \`uv run pytest -q tests/test_guarded.py\`
**Dependencies:** ${deps.length ? deps.map(d => `Task ${d}`).join(', ') : 'None'}

---
`
const ckPython = fs.existsSync(path.join(REPO, '.venv/bin/python')) ? path.join(REPO, '.venv/bin/python') : 'python3'
const CK = [ckPython, '-m', 'claude_kiro.cli']
const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })

// A real repo on `main` with an integration checkout at .ck/worktrees/demo-integration, and the execCk that runs the real ck in it.
function arbiterProject(deps) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'guard-ladder-'))
  git(root, 'init', '-q', '-b', 'main')
  git(root, 'config', 'user.name', 'Test User')
  git(root, 'config', 'user.email', 'test@example.com')
  const spec = path.join(root, 'specs/demo')
  fs.mkdirSync(spec, { recursive: true })
  fs.writeFileSync(path.join(root, 'specs/ck.json'), JSON.stringify({ verify: ['python -c pass'], guard: { tests: 'ask', requirements: 'ask', test_plan: 'ask' }, base: null }, null, 2) + '\n')
  fs.writeFileSync(path.join(root, 'specs/PRINCIPLES.md'), PRINCIPLES)
  fs.writeFileSync(path.join(spec, 'requirements.md'), '# Requirements: Demo\n\n- 1.1 WHEN a greeting is asked for, THE SYSTEM SHALL print it.\n')
  fs.writeFileSync(path.join(spec, 'design.md'), '# Design: Demo\n\nA greeting.\n')
  fs.writeFileSync(path.join(spec, 'test-plan.md'), '# Test Plan: Demo\n\n| TC-1 | 1.1 | I | greeting printed | CLI | tests/test_guarded.py |\n')
  fs.writeFileSync(path.join(spec, 'tasks.md'), `# Implementation Tasks: Demo\n\n**Status:** Not Started\n\n## Task Breakdown\n\n${Object.entries(deps).map(([n, d]) => taskBlock(n, d)).join('\n')}`)
  fs.writeFileSync(path.join(root, '.gitignore'), '.ck/\n.claude/worktrees/\n__pycache__/\n')
  git(root, 'add', '-A')
  git(root, 'commit', '-qm', 'init')
  execFileSync(CK[0], [...CK.slice(1), '-C', root, 'worktree', 'integration', 'demo', '--base', 'main'], { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  return { root, targetDir: path.join(root, '.ck/worktrees/demo-integration'), execCk: { cmd: CK, cwd: root } }
}

// A valid ruling that sends the implementer back with `text`, and the answers that are no ruling at all (6.10, P-16).
const CODE = 'KEEP-THE-TEST-CHANGE-THE-CODE'
const codeRuling = { claim: '1|test:tests/test_guarded.py::test_x', status: 'ruled', outcome: 'code', reasoning: 'the test states the requirement', principle: 'project:test-intent', code: CODE }
const FAILED_CALLS = ['throw', null, {}, 7, 'a string', [], { status: 3, reasoning: { bad: true } }]

const roles = (model, escalate, extra = {}) => ({
  implementer: { model, effort: 'medium', escalate, ...(extra.implementer || {}) },
  test_writer: { model, effort: 'medium', escalate },
  fixer: { model, effort: 'medium', escalate },
  reviewer: { model: 'haiku', effort: 'medium', enabled: false, rounds: 1 },
  arbiter: { model: 'opus', effort: 'high', risky_model: 'fable', risky_effort: 'high' },
  ...(extra.rest || {}),
})
const REVIEW_ON = { reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 2 } }
const attemptsOf = (s, n = '1') => s.events.filter(e => e.startsWith(`task ${n}`) && !e.endsWith(' end'))
const modelsOf = (s, n = '1') => attemptsOf(s, n).map(e => s.opts[e].model)
const GUARD_LABEL = / guard \d/
const arbiterEvents = (s, n = '1') => s.events.filter(e => new RegExp(`^arbiter task ${n}( risky)?$`).test(e))
// Every scenario runs on its own real git project; `opts` carries the fake project's options.
async function run(opts, args) {
  const deps = opts.deps
  const proj = arbiterProject(deps)
  const clock = new VirtualClock()
  const p = project({ ...opts, sleep: clock.sleep, external: q => clock.external(q), execCk: proj.execCk })
  const arbiterAgent = 'spec:arbiter'
  const result = await runWorkflow(SCRIPT, { agent: p.agent, clock, args: { spec: 'demo', root: proj.root, into: 'integrate/demo', targetDir: proj.targetDir, arbiterAgent, ...args } })
  return { p, root: proj.root, ...result }
}
const task = (result, n) => result.tasks.find(t => t.task === n)
const noteOf = root => `${path.join(root, '.ck/worktrees')}/demo-task-1.decision.md`

await test('TC-29 (6.2): a guard stop goes to the arbiter at once and climbs no ladder, whatever escalate says', async () => {
  const { p, result } = await run({ deps: { 1: [] }, guardStops: { 1: 'opus' }, rulings: { 1: [codeRuling] } }, { agents: roles('haiku', []) })
  assert.equal(arbiterEvents(p.state).length >= 1, true, p.state.events.join(', '))
  assert.ok(!p.state.events.some(e => GUARD_LABEL.test(e)), `no guard label: ${p.state.events.join(', ')}`)
  assert.equal(attemptsOf(p.state)[0], 'task 1')
  assert.ok(modelsOf(p.state).every(m => m === 'haiku'), `no attempt climbed to another model: ${modelsOf(p.state)}`)
  const first = p.state.events.findIndex(e => e === 'task 1')
  const next = p.state.events.slice(first + 1).find(e => !e.endsWith(' end') && !/^arbiter-/.test(e))
  assert.match(next, /^arbiter task 1( risky)?$/, 'the arbiter is the step right after the stop')
  assert.ok(!task(result, '1').escalations.some(e => e.stage === 'guard'), JSON.stringify(task(result, '1').escalations))
  assert.ok(!(result.escalated || []).some(e => e.task === '1' && (e.models || []).some(m => ['sonnet', 'opus', 'fable'].includes(m))), JSON.stringify(result.escalated))
  assert.ok(Array.isArray(result.rulings) && result.rulings.length >= 1, 'the run reports its ruling')
})

await test('TC-30 (6.2): the arbiter runs whichever model stopped; a risky task starts at its risky model and still goes to the arbiter', async () => {
  for (const model of ['sonnet', 'opus', 'fable']) {
    const { p } = await run({ deps: { 1: [] }, guardStops: { 1: 'always' }, rulings: { 1: [codeRuling] } }, { agents: roles(model, []) })
    assert.ok(arbiterEvents(p.state).length >= 1, `${model}: ${p.state.events.join(', ')}`)
    assert.ok(!p.state.events.some(e => GUARD_LABEL.test(e)), `${model}: ${p.state.events.join(', ')}`)
    assert.ok(modelsOf(p.state).every(m => m === model), `${model}: ${modelsOf(p.state)}`)
  }
  // A risky task starts at opus and never runs on sonnet; its guard stop goes to the arbiter, not up to fable.
  const agents = roles('haiku', ['sonnet', 'opus'], { implementer: { risky_model: null, complexity_routing: false } })
  const { p } = await run({ deps: { 1: [] }, guardStops: { 1: 'opus' }, risk: { 1: 'safety' }, rulings: { 1: [codeRuling] } }, { agents, risk: { 1: 'safety' } })
  assert.equal(modelsOf(p.state)[0], 'opus')
  assert.ok(!modelsOf(p.state).includes('fable') && !modelsOf(p.state).includes('sonnet'), modelsOf(p.state).join(','))
  assert.ok(arbiterEvents(p.state).length >= 1)
  assert.ok(!p.state.events.some(e => GUARD_LABEL.test(e)))
})

await test('TC-31 (6.2): a guard stop spends no escalate step: implementer.escalate is left alone', async () => {
  const { p, result } = await run({ deps: { 1: [] }, guardStops: { 1: 'sonnet' }, rulings: { 1: [codeRuling] } }, { agents: roles('haiku', ['opus']) })
  assert.ok(arbiterEvents(p.state).length >= 1)
  assert.ok(!modelsOf(p.state).includes('opus') && !modelsOf(p.state).includes('sonnet'), modelsOf(p.state).join(','))
  assert.deepEqual(task(result, '1').escalations, [])
})

await test('TC-32: ordinary failures keep the escalate sequence, never use the guard ladder and never call the arbiter', async () => {
  const { p, result } = await run({ deps: { 1: [] }, failUntil: { 1: 3 } }, { maxRetries: 1, agents: roles('haiku', ['sonnet', 'opus']) })
  assert.deepEqual(modelsOf(p.state), ['haiku', 'haiku', 'sonnet', 'opus'])
  assert.deepEqual(task(result, '1').escalations, [{ stage: 'retry', model: 'sonnet' }, { stage: 'retry', model: 'opus' }])
  assert.ok(!p.state.events.some(e => GUARD_LABEL.test(e)))
  assert.ok(!p.state.events.some(e => e.startsWith('arbiter')), 'a retry that passes never reaches the arbiter (6.6)')

  const red = await run({ deps: { 1: [] }, checkRed: ['1'] }, { agents: roles('haiku', ['sonnet']) })
  assert.ok(!red.p.state.events.some(e => GUARD_LABEL.test(e)))
  assert.ok(!task(red.result, '1').escalations.some(e => e.stage === 'guard'))

  const rev = await run({ deps: { 1: [] }, reviews: { 1: ['changes', 'approve'] } }, { agents: roles('haiku', ['sonnet'], { rest: REVIEW_ON }) })
  assert.ok(!rev.p.state.events.some(e => GUARD_LABEL.test(e)))
  assert.ok(!task(rev.result, '1').escalations.some(e => e.stage === 'guard'))
})

await test('TC-33 (6.10): an arbiter that gives nothing valid parks the task once with the quoted test and requirement, and no ladder runs', async () => {
  for (const answer of FAILED_CALLS) {
    const { p, result } = await run({ deps: { 1: [] }, guardStops: { 1: 'always' }, rulings: { 1: [answer] } }, { agents: roles('fable', []) })
    assert.equal(attemptsOf(p.state).length, 1, `${JSON.stringify(answer)}: ${attemptsOf(p.state)}`)
    assert.equal(arbiterEvents(p.state).length, 1, `the arbiter was called once: ${JSON.stringify(answer)}`)
    assert.ok(!p.state.events.some(e => GUARD_LABEL.test(e)), JSON.stringify(answer))
    assert.equal(p.state.events.filter(e => e === 'park 1').length, 1, JSON.stringify(answer))
    const prompt = p.state.prompts['park 1']
    assert.match(prompt, /ck worktree park demo 1/)
    assert.ok(prompt.includes('tests/test_guarded.py::test_x'), 'quotes the test')
    assert.ok(prompt.includes('1.1'), 'quotes the requirement')
    assert.deepEqual(result.merged, [])
  }
})

await test('TC-34 (6.9, 6.10): a claim sent to me blocks dependents only; needs-input is listed apart from merged, failed and blocked', async () => {
  const { p, result } = await run({ deps: { 1: [], 2: ['1'], 3: [] }, guardStops: { 1: 'always' }, rulings: { 1: ['throw'] } }, { agents: roles('fable', []) })
  assert.ok(arbiterEvents(p.state).length >= 1, 'the claim went to the arbiter first')
  assert.ok(!p.state.events.includes('task 2'), 'task 2 never starts')
  assert.deepEqual(result.merged, ['3'])
  assert.equal(result.needsInput.length, 1)
  const ni = result.needsInput[0]
  assert.equal(ni.task, '1')
  assert.equal(ni.test, 'tests/test_guarded.py::test_x')
  assert.equal(ni.requirement, '1.1')
  assert.ok('decision' in ni, 'carries the decision needed')
  assert.equal(ni.action, 'park')
  assert.ok(!result.failed.some(f => f.task === '1'))
  assert.ok(!result.blockedOn.some(b => b.task === '1'))
  assert.ok(!result.merged.includes('1'))
  assert.ok(result.blocked.some(b => b.task === '2' && b.on.includes('1')), JSON.stringify(result.blocked))
  assert.notEqual(result.outcome, 'complete')
  assert.equal(task(result, '1').parked, true)
  assert.ok(!p.state.events.some(e => GUARD_LABEL.test(e)))
})

await test('TC-35: a missing or failed notification reaches the result and the run continues', async () => {
  const a = await run({ deps: { 1: [], 2: [] }, guardStops: { 1: 'always' }, rulings: { 1: ['throw'] }, parkOutcome: { parked: true, notification: 'not_configured', detail: 'set run.notify_command with ck agents set run.notify_command' } }, { agents: roles('fable', []) })
  assert.ok(arbiterEvents(a.p.state).length >= 1, 'the claim went to the arbiter first')
  assert.equal(a.result.notifications.notConfigured, true)
  assert.match(a.result.notifications.hint, /ck agents set run\.notify_command/)
  assert.deepEqual(a.result.merged, ['2'])

  const b = await run({ deps: { 1: [], 2: [] }, guardStops: { 1: 'always' }, rulings: { 1: ['throw'] }, parkOutcome: { parked: true, notification: 'failed', detail: 'exit 3' } }, { agents: roles('fable', []) })
  assert.equal(b.result.notifications.failed[0].detail, 'exit 3')
  assert.equal(b.result.notifications.failed[0].task, '1')
  assert.deepEqual(b.result.merged, ['2'])
})

await test('TC-36: an interactive run parks nothing and lists the entry with action ask', async () => {
  const { p, result } = await run({ deps: { 1: [] }, guardStops: { 1: 'always' }, rulings: { 1: ['throw'] } }, { interactive: true, agents: roles('fable', []) })
  assert.ok(arbiterEvents(p.state).length >= 1, 'the claim went to the arbiter first')
  assert.ok(!p.state.events.some(e => e.startsWith('park')), 'no park agent runs')
  assert.equal(result.needsInput[0].action, 'ask')
  assert.equal(result.needsInput[0].test, 'tests/test_guarded.py::test_x')
  assert.equal(result.needsInput[0].requirement, '1.1')
})

await test('TC-79 (6.2): a guard stop spends no retry or escalate step; a revise-call guard stop goes to the arbiter and the revision runs again with its ruling', async () => {
  const { p, result } = await run({ deps: { 1: [] }, guardStops: { 1: 'sonnet' }, rulings: { 1: ['throw'] } }, { maxRetries: 2, agents: roles('haiku', ['sonnet', 'opus']) })
  assert.ok(!p.state.events.some(e => / retry \d/.test(e)), p.state.events.join(', '))
  assert.ok(!p.state.events.some(e => GUARD_LABEL.test(e)), p.state.events.join(', '))
  assert.deepEqual(attemptsOf(p.state), ['task 1'])
  assert.ok(!task(result, '1').escalations.some(e => e.stage === 'retry' || e.stage === 'guard'))
  assert.deepEqual(result.merged, [])

  const r = await run({ deps: { 1: [] }, guardStops: { 1: 'sonnet' }, guardStopIn: { 1: 'revise' }, reviews: { 1: ['changes', 'approve'] }, rulings: { 1: [codeRuling] } }, { agents: roles('haiku', ['sonnet', 'opus'], { rest: REVIEW_ON }) })
  const revises = r.p.state.events.filter(e => / revise /.test(e) && !e.endsWith(' end'))
  assert.ok(revises.length >= 2, `a revise call ran, then again after the ruling: ${r.p.state.events.join(', ')}`)
  assert.ok(arbiterEvents(r.p.state).length >= 1, 'the revise guard stop reached the arbiter')
  assert.ok(!r.p.state.events.some(e => GUARD_LABEL.test(e)), 'no guard attempt ran')
  assert.ok(revises.some(l => r.p.state.prompts[l].includes(CODE)), 'a re-run revision carries the ruling text')
  assert.ok(!r.result.failed.some(f => f.task === '1'))
})

await test('TC-80: task, retry, revise and resumed-after-ruling prompts read the decision note first', async () => {
  const q = await run({ deps: { 1: [] }, failUntil: { 1: 1 }, reviews: { 1: ['changes', 'approve'] } }, { maxRetries: 1, agents: roles('haiku', ['sonnet'], { rest: REVIEW_ON }) })
  const qLabels = Object.keys(q.p.state.prompts).filter(l => /^task 1/.test(l))
  const qKinds = new Set(qLabels.map(l => (l.match(/ (retry|revise|guard)\b/) || [, 'task'])[1]))
  assert.ok(qKinds.has('retry') && qKinds.has('revise'), [...qKinds].join(','))

  // A guard stop now goes to the arbiter; the attempt that resumes after its ruling reads the note first as well.
  const p = await run({ deps: { 1: [] }, guardStops: { 1: 'haiku' }, rulings: { 1: [codeRuling] } }, { agents: roles('haiku', ['sonnet'], { rest: REVIEW_ON }) })
  const labels = Object.keys(p.p.state.prompts).filter(l => /^task 1/.test(l))
  assert.ok(labels.length >= 2, `a first attempt and an attempt resumed after the ruling: ${labels.join(', ')}`)
  assert.ok(!labels.some(l => GUARD_LABEL.test(l)))
  const entries = [...labels.map(l => [l, p.p.state.prompts[l], noteOf(p.root)]), ...qLabels.map(l => [l, q.p.state.prompts[l], noteOf(q.root)])]
  for (const [l, prompt, note] of entries) {
    const at = prompt.indexOf(note)
    assert.ok(at >= 0, `${l} names ${note}`)
    assert.ok(/read/i.test(prompt.slice(Math.max(0, at - 160), at + 160)), `${l} says to read it`)
    assert.ok(/follow/i.test(prompt.slice(at, at + 400)), `${l} says to follow it`)
    const gate = prompt.indexOf('gate')
    assert.ok(gate < 0 || at < gate, `${l} reads it before the work starts`)
  }
})

await test('a guard stop spends no gate-retry budget: the arbiter runs, no guard attempt, no retry on the start model', async () => {
  const { p } = await run({ deps: { 1: [] }, guardStops: { 1: 'haiku' }, failUntil: { 1: 3 }, rulings: { 1: [codeRuling] } }, { maxRetries: 1, agents: roles('haiku', []) })
  // The stub stops every call at or below the ceiling, so the attempt resumed after the ruling stops again and its repeat goes to me: only the prefix is the point.
  const labels = p.state.events.filter(e => !e.endsWith(' end') && !/^arbiter-/.test(e))
  const at = labels.indexOf('task 1')
  assert.ok(at >= 0, labels.join(', '))
  assert.match(labels[at + 1], /^arbiter task 1( risky)?$/, labels.join(', '))
  assert.ok(!labels.some(l => GUARD_LABEL.test(l) || / retry \d/.test(l)), labels.join(', '))
})
