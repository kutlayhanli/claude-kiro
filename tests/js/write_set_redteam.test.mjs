// TC-120 / TC-124 red team (Task 91): adversarial cases for 13.1 "overlapping tasks SHALL not run at the same time"
// and 13.6 "holding a task SHALL not change its dependencies". Written from the requirements only.
// Run: node tests/js/write_set_redteam.test.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { runWorkflow } from './load_workflow.mjs'
import { loadDecisions } from './load_decisions.mjs'
import { project } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.join(here, '../..')
const SCRIPT = path.join(REPO, 'plugin/workflows/spec-implement.js')
const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
const ROLES = {
  implementer: { model: 'sonnet', effort: 'medium' },
  test_writer: { model: 'opus', effort: 'high' },
  reviewer: { model: 'opus', effort: 'high', enabled: false, rounds: 1 },
  fixer: { model: 'sonnet', effort: 'medium' },
  orchestrator: { model: 'sonnet', effort: 'low' },
  resolver: { model: 'opus', effort: 'high' },
}

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

const go = (p, waves, extraArgs = {}) => {
  const ids = Object.keys(p.state.deps)
  return runWorkflow(SCRIPT, {
    agent: p.agent,
    slots: 6,
    args: { ...baseArgs, wave: waves ?? Object.fromEntries(ids.map(n => [n, 1])), tracks: Object.fromEntries(ids.map(n => [n, 'impl'])), agents: ROLES, ...extraArgs },
  })
}

// In-flight intervals of every `task N ...` agent call (first attempt and retries), from the trace.
function intervals(trace) {
  const open = {}
  const out = []
  for (const e of trace) {
    const m = e.label.match(/^task (\d+)/)
    if (!m) continue
    const key = e.label
    if (e.ev === 'start') open[key] = e.t
    if (e.ev === 'end') out.push({ task: m[1], label: key, from: open[key], to: e.t })
  }
  return out
}
// Symmetric closure: an asymmetric `overlaps` map still names a pair that overlaps.
function pairsOf(overlaps) {
  const set = new Set()
  for (const [a, bs] of Object.entries(overlaps)) for (const b of bs) if (a !== b) set.add([a, b].sort().join('~'))
  return [...set].map(s => s.split('~'))
}
function assertNeverTogether(trace, overlaps) {
  const iv = intervals(trace)
  for (const [a, b] of pairsOf(overlaps)) {
    for (const x of iv.filter(i => i.task === a)) for (const y of iv.filter(i => i.task === b)) {
      assert.ok(x.to <= y.from || y.to <= x.from, `tasks ${a} (${x.label}) and ${b} (${y.label}) ran at the same time: ${JSON.stringify([x, y])}`)
    }
  }
}
const ran = (trace, n) => intervals(trace).some(i => i.task === n)

// ---- 13.1 against the real workflow: in-flight intervals from the trace

await test('tc120_redteam asymmetric overlaps map (a -> b only) still keeps the pair apart', async () => {
  for (const overlaps of [{ 2: ['3'] }, { 3: ['2'] }]) {
    const p = project({ deps: { 2: [], 3: [] }, durations: { 2: 20, 3: 20 }, overlaps })
    const { trace, result } = await go(p)
    assertNeverTogether(trace, overlaps)
    assert.deepEqual([...result.merged].sort(), ['2', '3'], JSON.stringify(overlaps))
  }
})

await test('tc120_redteam a task listing itself in overlaps is not held forever', async () => {
  const p = project({ deps: { 2: [], 3: [] }, durations: { 2: 10, 3: 10 }, overlaps: { 2: ['2', '3'], 3: ['2'] } })
  const { trace, result } = await go(p)
  assertNeverTogether(trace, { 2: ['3'] })
  assert.deepEqual([...result.merged].sort(), ['2', '3'])
})

await test('tc120_redteam three pairwise overlapping tasks run one after another, and a disjoint task still runs beside them', async () => {
  const overlaps = { 2: ['3', '4'], 3: ['2', '4'], 4: ['2', '3'] }
  const p = project({ deps: { 2: [], 3: [], 4: [], 5: [] }, durations: { 2: 25, 3: 25, 4: 25, 5: 25 }, overlaps })
  const { trace, result } = await go(p)
  assertNeverTogether(trace, overlaps)
  assert.deepEqual([...result.merged].sort(), ['2', '3', '4', '5'])
  const iv = intervals(trace)
  const five = iv.find(i => i.task === '5')
  assert.ok(iv.some(i => i.task !== '5' && i.from < five.to && five.from < i.to), 'disjoint Task 5 shared time with an overlapping one: the hold must not serialise everything')
})

await test('tc120_redteam three tasks overlapping as a chain (2-3, 3-4) keep each linked pair apart', async () => {
  const overlaps = { 2: ['3'], 3: ['2', '4'], 4: ['3'] }
  const p = project({ deps: { 2: [], 3: [], 4: [] }, durations: { 2: 20, 3: 20, 4: 20 }, overlaps })
  const { trace, result } = await go(p)
  assertNeverTogether(trace, overlaps)
  assert.deepEqual([...result.merged].sort(), ['2', '3', '4'])
})

await test('tc120_redteam overlap with a task whose dependency lands later: the late task never starts beside its overlapping peer', async () => {
  const overlaps = { 2: ['4'], 4: ['2'] }
  const p = project({ deps: { 1: [], 2: [], 4: ['1'] }, durations: { 1: 5, 2: 40, 4: 10 }, overlaps })
  const { trace, result } = await go(p)
  assertNeverTogether(trace, overlaps)
  assert.deepEqual([...result.merged].sort(), ['1', '2', '4'])
})

await test('tc120_redteam an overlap that only shows up in a later plan answer holds the task that becomes ready then', async () => {
  const overlaps = { 2: ['3'], 3: ['2'] }
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'] }, durations: { 1: 5, 2: 25, 3: 25 }, overlaps, planSeq: [{ overlaps: {} }] })
  const { trace, result } = await go(p)
  assertNeverTogether(trace, overlaps)
  assert.deepEqual([...result.merged].sort(), ['1', '2', '3'])
})

await test('tc120_redteam a retry of the running task keeps its overlapping peer waiting through every attempt', async () => {
  const overlaps = { 2: ['3'], 3: ['2'] }
  const p = project({ deps: { 2: [], 3: [] }, durations: { 2: 15, 3: 15 }, failUntil: { 2: 1 }, overlaps })
  const { trace, result } = await go(p)
  assert.ok(intervals(trace).filter(i => i.task === '2').length >= 2, 'Task 2 retried: ' + p.state.events.join(' | '))
  assertNeverTogether(trace, overlaps)
  assert.deepEqual([...result.merged].sort(), ['2', '3'])
})

await test('tc120_redteam a retry of the held side: the second attempt of Task 3 is not run beside Task 2', async () => {
  const overlaps = { 2: ['3'], 3: ['2'] }
  const p = project({ deps: { 2: [], 3: [] }, durations: { 2: 20, 3: 10 }, failUntil: { 3: 1 }, overlaps })
  const { trace } = await go(p)
  assertNeverTogether(trace, overlaps)
})

await test('tc120_redteam a held task is released when its blocker parks instead of merging, and never overlaps it before', async () => {
  const overlaps = { 2: ['3'], 3: ['2'] }
  // Task 2's landing comes back PROTECTED, so the run parks it for my approval instead of merging it (9.8, 2.13)
  const p = project({ deps: { 2: [], 3: [], 4: ['2'] }, durations: { 2: 20, 3: 10 }, protectedTasks: ['2'], overlaps })
  const { trace, result } = await go(p)
  assertNeverTogether(trace, overlaps)
  assert.ok(p.state.parks.includes('2'), 'Task 2 parked: ' + JSON.stringify(p.state.parks))
  assert.ok(ran(trace, '3'), 'the held Task 3 started once Task 2 parked (13.5): ' + p.state.events.join(' | '))
  assert.ok(result.merged.includes('3'))
  assert.ok(!ran(trace, '4'), 'a dependent of the parked task never starts')
})

await test('tc120_redteam the blocker is a task that finishes in-progress with no commits: the held task still starts later, apart', async () => {
  const overlaps = { 2: ['3'], 3: ['2'] }
  const p = project({ deps: { 2: [], 3: [] }, durations: { 2: 15, 3: 10 }, noCommitsFirst: ['2'], overlaps })
  const { trace, result } = await go(p)
  assertNeverTogether(trace, overlaps)
  assert.deepEqual([...result.merged].sort(), ['2', '3'])
})

await test('tc120_redteam overlap holds also apply when only one slot-worth of room is free: slots 2, three overlapping and one disjoint', async () => {
  const overlaps = { 2: ['3'], 3: ['2'] }
  const p = project({ deps: { 2: [], 3: [], 4: [], 5: [] }, durations: { 2: 15, 3: 15, 4: 15, 5: 15 }, overlaps })
  const { trace, result } = await runWorkflow(SCRIPT, {
    agent: p.agent, slots: 2,
    args: { ...baseArgs, wave: { 2: 1, 3: 1, 4: 1, 5: 1 }, tracks: { 2: 'impl', 3: 'impl', 4: 'impl', 5: 'impl' }, agents: ROLES, concurrency: 2 },
  })
  assertNeverTogether(trace, overlaps)
  assert.deepEqual([...result.merged].sort(), ['2', '3', '4', '5'])
})

await test('tc120_redteam the system output fed back: a second run over the same plan answer gives the same non-overlapping schedule', async () => {
  const overlaps = { 2: ['3', '4'], 3: ['2'], 4: ['2'] }
  for (let i = 0; i < 2; i++) {
    const p = project({ deps: { 2: [], 3: [], 4: [] }, durations: { 2: 10, 3: 10, 4: 10 }, overlaps })
    const { trace, result } = await go(p)
    assertNeverTogether(trace, overlaps)
    assert.deepEqual([...result.merged].sort(), ['2', '3', '4'])
  }
})

// ---- 13.6: holding does not touch the dependencies

await test('tc124_redteam holding for overlap changes no dependency: no relink, no tasks.md edit asked of any agent', async () => {
  const overlaps = { 2: ['3'], 3: ['2'], 4: ['2', '3'] }
  const deps = { 2: [], 3: [], 4: ['1'], 1: [] }
  const p = project({ deps, durations: { 1: 5, 2: 20, 3: 20, 4: 10 }, overlaps })
  const { result } = await go(p)
  assert.deepEqual(p.state.deps, deps, 'the plan deps are exactly what the project had')
  assert.deepEqual(p.state.relinks, [])
  assert.deepEqual([...p.state.relinked], [])
  assert.deepEqual([...result.merged].sort(), ['1', '2', '3', '4'])
  for (const label of p.state.events) assert.ok(!/relink/i.test(label), 'a relink step ran: ' + label)
})

await test('tc124_redteam the held task never gains the blocker as a dependency in any later plan call', async () => {
  const overlaps = { 2: ['3'], 3: ['2'] }
  const p = project({ deps: { 2: [], 3: [] }, durations: { 2: 15, 3: 15 }, overlaps })
  const answers = []
  const inner = p.agent
  const spy = async (prompt, opts) => { const r = await inner(prompt, opts); if (/^plan/.test(opts.label)) answers.push(r); return r }
  await runWorkflow(SCRIPT, { agent: spy, slots: 6, args: { ...baseArgs, wave: { 2: 1, 3: 1 }, tracks: { 2: 'impl', 3: 'impl' }, agents: ROLES } })
  assert.ok(answers.length > 0)
  // a plan call is a thin ck call, so its answer is the CK_CALL envelope {exitCode, json} around the plan (19.1)
  for (const a of answers) assert.deepEqual((a.json ?? a).deps, { 2: [], 3: [] })
})

await test('tc124_redteam a blocker that parks leaves the held task without that dependency in the run result', async () => {
  const overlaps = { 2: ['3'], 3: ['2'] }
  const p = project({ deps: { 2: [], 3: [] }, durations: { 2: 15, 3: 10 }, protectedTasks: ['2'], overlaps })
  await go(p)
  assert.deepEqual(p.state.deps, { 2: [], 3: [] })
  assert.deepEqual(p.state.relinks, [])
})

// ---- 13.1 / 13.6 against the real `ck plan` over a real tasks.md: surface forms of the same write-set

const python = fs.existsSync(path.join(REPO, '.venv/bin/python')) ? path.join(REPO, '.venv/bin/python') : 'python3'
const ck = (cwd, ...args) => {
  const env = { ...process.env, PYTHONPATH: path.join(REPO, 'src') + path.delimiter + (process.env.PYTHONPATH ?? ''), PYTHONDONTWRITEBYTECODE: '1' }
  return execFileSync(python, ['-m', 'claude_kiro.cli', ...args], { cwd, env, encoding: 'utf8' })
}
const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8' })
function block(num, files, deps = 'None') {
  return [`### Task ${num}: T${num}`, '**Status:** Not Started', '**Track:** impl', '**Files:**', ...files.map(f => `- ${f}`), `**Dependencies:** ${deps}`, '', '---', ''].join('\n') + '\n'
}
function demoRepo(blocks) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ws-redteam-'))
  git(root, 'init', '-q', '-b', 'main')
  git(root, 'config', 'user.email', 't@example.com')
  git(root, 'config', 'user.name', 'Test')
  const spec = path.join(root, 'specs', 'demo')
  fs.mkdirSync(spec, { recursive: true })
  fs.writeFileSync(path.join(spec, 'tasks.md'), '# Implementation Tasks: Demo\n\n## Task Breakdown\n\n' + blocks.join(''))
  fs.writeFileSync(path.join(spec, 'requirements.md'), '# Feature: Demo\n\n- 1.1 WHEN a THE SYSTEM SHALL b\n')
  fs.writeFileSync(path.join(spec, 'design.md'), '# Design: Demo\n')
  fs.writeFileSync(path.join(spec, 'test-plan.md'), '# Test Plan: Demo\n')
  git(root, 'add', '-A')
  git(root, 'commit', '-qm', 'base')
  return { root, tasksMd: path.join(spec, 'tasks.md') }
}
const planOf = root => JSON.parse(ck(root, 'plan', 'demo', '--json'))
const pairSet = overlaps => new Set(pairsOf(overlaps ?? {}).map(p => p.join('~')))
const hasPair = (plan, a, b) => pairSet(plan.overlaps).has([a, b].sort().join('~'))

// A Files entry is a backticked path (`- \`path\` - reason`, the task-list format every reader shares: an
// unbackticked line is not an entry, so a task with only such lines has no write-set, 13.4); a directory is written
// with a trailing `/` (design: `_norm_entry`, `dir/` means `dir/**`); paths are case-sensitive as on the platform.
const SURFACE = [
  ['./ prefix', '`./src/a.py` - x', '`src/a.py` - y'],
  ['backticks with description suffix', '`src/a.py` - the module', '`src/a.py`'],
  ['trailing slash directory', '`src/` - everything', '`src/a.py` - one file'],
  ['double star against single star', '`src/**` - all', '`src/*.py` - flat'],
  ['single star against a file', '`src/*.py`', '`src/a.py`'],
  ['double star against a deep file', '`src/**`', '`src/x/y.py`'],
  ['doubled slash', '`src//a.py`', '`src/a.py`'],
  ['dot segment', '`src/./a.py`', '`src/a.py`'],
]
for (const [label, fa, fb] of SURFACE) {
  await test(`tc120_redteam real ck plan: ${label} is an overlap, in both directions`, () => {
    const { root } = demoRepo([block(1, [fa]), block(2, [fb])])
    const plan = planOf(root)
    assert.ok(hasPair(plan, '1', '2'), JSON.stringify(plan.overlaps))
    const o = plan.overlaps
    assert.ok((o['1'] ?? []).includes('2') && (o['2'] ?? []).includes('1'), 'the map is symmetric: ' + JSON.stringify(o))
  })
}

await test('tc120_redteam real ck plan: near-miss paths are not overlaps (prefix, suffix, sibling directory)', () => {
  const { root } = demoRepo([
    block(1, ['`src/a.py`']), block(2, ['`src/ab.py`']), block(3, ['`src2/a.py`']), block(4, ['`src/a.py.bak`']), block(5, ['`tests/src/a.py`']), block(6, ['`src/*.js`']),
  ])
  const plan = planOf(root)
  assert.ok('overlaps' in plan, 'plan reports an overlaps map')
  const pairs = [...pairSet(plan.overlaps)]
  assert.deepEqual(pairs, [], 'no near miss is held: ' + JSON.stringify(plan.overlaps))
})

await test('tc120_redteam real ck plan: three pairwise overlapping tasks report all three pairs', () => {
  const { root } = demoRepo([block(1, ['`src/a.py`']), block(2, ['`./src/a.py` - again']), block(3, ['`src/*.py`'])])
  const plan = planOf(root)
  for (const [a, b] of [['1', '2'], ['1', '3'], ['2', '3']]) assert.ok(hasPair(plan, a, b), `${a}~${b}: ` + JSON.stringify(plan.overlaps))
})

await test('tc120_redteam real ck plan: a task with no Files list or an empty one is never in overlaps', () => {
  const { root } = demoRepo([block(1, ['`src/a.py`']), block(2, []), block(3, ['`src/a.py`'])])
  const plan = planOf(root)
  assert.ok(hasPair(plan, '1', '3'))
  for (const [k, vs] of Object.entries(plan.overlaps ?? {})) assert.ok(k !== '2' && !vs.includes('2'), JSON.stringify(plan.overlaps))
})

await test('tc120_redteam real ck plan: an overlap between a done task and a pending one is not a hold', () => {
  const done = block(1, ['`src/a.py`']).replace('Not Started', 'Done')
  const { root } = demoRepo([done, block(2, ['`src/a.py`'])])
  const plan = planOf(root)
  // ck plan's overlaps map covers the task list; the hold is the workflow's decision over the ready and in-flight
  // tasks (13.1, 13.2). Fed the real plan answer, it starts the pending task and holds nothing: the done task is
  // neither ready nor in flight.
  assert.ok(!plan.ready.includes('1') && plan.ready.includes('2'), JSON.stringify(plan.ready))
  const { writeSetHold } = loadDecisions({ extra: ['writeSetHold'] })
  assert.deepEqual(writeSetHold({ ready: plan.ready, active: [], overlaps: plan.overlaps }), { start: ['2'], held: [] })
})

await test('tc124_redteam real ck plan leaves tasks.md byte-identical, twice, and dependencies are the written ones', () => {
  const { root, tasksMd } = demoRepo([block(1, ['`src/a.py`']), block(2, ['`./src/a.py`'], 'None'), block(3, ['`src/**`'], 'Task 1')])
  const before = fs.readFileSync(tasksMd)
  const first = planOf(root)
  assert.ok(hasPair(first, '1', '2') && hasPair(first, '2', '3'), JSON.stringify(first.overlaps))
  assert.ok(fs.readFileSync(tasksMd).equals(before), 'first plan changed tasks.md')
  const second = planOf(root)
  assert.ok(fs.readFileSync(tasksMd).equals(before), 'second plan changed tasks.md')
  assert.deepEqual(second.deps, first.deps)
  assert.deepEqual(first.deps['1'], [])
  assert.deepEqual(first.deps['2'], [], 'the overlap did not become a dependency of 2')
  assert.deepEqual(first.deps['3'], ['1'])
  assert.equal(git(root, 'status', '--short', '--', 'specs').trim(), '', 'no change in the spec directory')
})

await test('tc124_redteam ck plan and ck lint over overlapping write-sets leave tasks.md byte-identical', () => {
  const { root, tasksMd } = demoRepo([block(1, ['`src/a.py`']), block(2, ['`src/a.py`'])])
  const before = fs.readFileSync(tasksMd)
  for (const args of [['plan', 'demo', '--json'], ['lint', 'demo', '--json']]) {
    try { ck(root, ...args) } catch (e) { /* lint may exit non-zero on warnings */ }
    assert.ok(fs.readFileSync(tasksMd).equals(before), args.join(' ') + ' changed tasks.md')
  }
})
