// TC-67 red team (Task 85): adversarial cases for 7.4, "WHILE a cross-test finding is open, THE SYSTEM SHALL not
// start impl tasks that the tests it names verify". Written from the requirement only.
// Run: node tests/js/cross_test_redteam.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { loadDecisions } from './load_decisions.mjs'
import { project } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')
const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
const ROLES = {
  implementer: { model: 'sonnet', effort: 'medium' },
  test_writer: { model: 'opus', effort: 'high' },
  reviewer: { model: 'opus', effort: 'high', enabled: false, rounds: 1, cross_test: true },
  fixer: { model: 'sonnet', effort: 'medium' },
  orchestrator: { model: 'sonnet', effort: 'low' },
  resolver: { model: 'opus', effort: 'high' },
}

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}
const decisions = () => loadDecisions({ extra: ['heldByCrossTest'] })
const held = (findings, testVerifies) => [...decisions().heldByCrossTest({ findings, testVerifies })].sort()
const blocking = (f) => ({ severity: 'blocking', issue: 'tests disagree', ...f })

// ---- decision seam: heldByCrossTest({findings, testVerifies}) -> Set<task>

await test('tc67_redteam finding naming the file only holds the tasks that file verifies', () => {
  assert.deepEqual(held([blocking({ file: 'tests/test_a.py' })], { 'tests/test_a.py': ['3'], 'tests/test_b.py': ['4'] }), ['3'])
})

await test('tc67_redteam a finding naming a node id holds the tasks of its file, with parametrize and class parts', () => {
  const tv = { 'tests/test_a.py': ['3'], 'tests/test_b.py': ['4'] }
  assert.deepEqual(held([blocking({ file: 'tests/test_a.py', test: 'tests/test_a.py::test_x' })], tv), ['3'])
  assert.deepEqual(held([blocking({ file: 'tests/test_a.py', test: 'tests/test_a.py::TestK::test_x[a-1]' })], tv), ['3'])
  assert.deepEqual(held([blocking({ test: 'tests/test_a.py::TestK::test_x[a::b]' })], tv), ['3'], 'node id alone is enough')
})

await test('tc67_redteam a bare function name next to the file still resolves through the file', () => {
  assert.deepEqual(held([blocking({ file: 'tests/test_a.py', test: 'test_x' })], { 'tests/test_a.py': ['3'], 'tests/test_b.py': ['4'] }), ['3'])
})

await test('tc67_redteam a finding that names a test with no file and no node id does not throw and holds nothing it cannot place', () => {
  const r = held([blocking({ test: 'test_x' })], { 'tests/test_a.py': ['3'] })
  assert.ok(r.length === 0 || r.join() === '3', 'either nothing or (conservatively) the only candidate: ' + r)
})

await test('tc67_redteam surface forms of the same file hold the same tasks: ./ prefix, backslashes, doubled slashes, trailing space', () => {
  const tv = { 'tests/test_a.py': ['3'], 'tests/test_b.py': ['4'] }
  for (const file of ['./tests/test_a.py', 'tests\\test_a.py', 'tests//test_a.py', ' tests/test_a.py ', './tests/./test_a.py']) {
    assert.deepEqual(held([blocking({ file })], tv), ['3'], JSON.stringify(file))
  }
})

await test('tc67_redteam a surface form on the testVerifies side (./ prefix) still matches a clean finding', () => {
  assert.deepEqual(held([blocking({ file: 'tests/test_a.py' })], { './tests/test_a.py': ['3'], 'tests/test_b.py': ['4'] }), ['3'])
})

await test('tc67_redteam different casing of the file name is held, not missed (holding too much is safe, starting is not)', () => {
  assert.deepEqual(held([blocking({ file: 'Tests/Test_A.py' })], { 'tests/test_a.py': ['3'], 'tests/test_b.py': ['4'] }), ['3'])
})

await test('tc67_redteam near-miss file names hold nothing: prefix, suffix and sibling directories', () => {
  const tv = { 'tests/test_a.py': ['3'] }
  for (const file of ['tests/test_a.pyc', 'tests/test_a', 'tests/test_ab.py', 'xtests/test_a.py', 'other/tests/test_a.py', 'tests/test_a.py/x', 'tests']) {
    assert.deepEqual(held([blocking({ file })], tv), [], file)
  }
})

await test('tc67_redteam a finding naming tests of two test tasks holds the union of what each verifies', () => {
  const tv = { 'tests/test_a.py': ['3', '5'], 'tests/test_b.py': ['5', '6'], 'tests/test_c.py': ['7'] }
  assert.deepEqual(held([blocking({ file: 'tests/test_a.py' }), blocking({ file: 'tests/test_b.py', test: 'tests/test_b.py::t' })], tv), ['3', '5', '6'])
})

await test('tc67_redteam testVerifies with extra entries: files no finding names never hold their tasks', () => {
  const tv = { 'tests/test_a.py': ['3'], 'tests/zzz.py': ['9'], '': ['8'], 'tests/test_a.py::x': ['7'] }
  assert.deepEqual(held([blocking({ file: 'tests/test_a.py' })], tv), ['3'])
})

await test('tc67_redteam testVerifies with missing entries, empty, null or undefined: nothing held, no throw', () => {
  const f = [blocking({ file: 'tests/test_a.py' })]
  for (const tv of [{}, { 'tests/test_b.py': ['4'] }, { 'tests/test_a.py': [] }, null, undefined]) assert.deepEqual(held(f, tv), [], JSON.stringify(tv))
})

await test('tc67_redteam no findings, null findings or findings without a file hold nothing', () => {
  const tv = { 'tests/test_a.py': ['3'] }
  for (const f of [[], null, undefined, [blocking({})], [blocking({ file: '' })]]) assert.deepEqual(held(f, tv), [], JSON.stringify(f))
})

await test('tc67_redteam the result is a Set of task ids with no duplicates, and the inputs are not changed', () => {
  const findings = [blocking({ file: 'tests/test_a.py' }), blocking({ file: './tests/test_a.py' })]
  const tv = { 'tests/test_a.py': ['3', '3'], 'tests/test_b.py': ['3'] }
  const before = JSON.stringify([findings, tv])
  const r = decisions().heldByCrossTest({ findings, testVerifies: tv })
  assert.ok(r instanceof Set)
  assert.deepEqual([...r], ['3'])
  assert.equal(JSON.stringify([findings, tv]), before)
})

await test('tc67_redteam the system output fed back: a held set computed again from the same input is the same set', () => {
  const findings = [blocking({ file: 'tests/test_a.py' })]
  const tv = { 'tests/test_a.py': ['3'] }
  const once = held(findings, tv)
  assert.deepEqual(held(findings, tv), once)
  assert.deepEqual(held(findings.concat(findings), tv), once)
})

// ---- the real workflow: no `task N` request while the finding is open

const FIX = 'tests/test_a.py'
// 1, 2 test tasks (wave 1), 3 impl verifying FIX (needs 1 and 2), 4 unrelated and ready from the start, 6 depends on 3.
const scenario = (finding, extra = {}, testVerifies = { [FIX]: ['3'] }) => project({
  deps: { 1: [], 2: [], 3: ['1', '2'], 4: [], 6: ['3'] },
  durations: { 1: 5, 2: 5, 4: 5 },
  crossTest: { verdict: 'changes', findings: [blocking(finding)], summary: 'drift' },
  testVerifies,
  ...extra,
})
const go = (p, extraArgs = {}) => runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, wave: { 1: 1, 2: 1, 3: 2, 4: 1, 6: 3 }, tracks: { 1: 'test', 2: 'test', 3: 'impl', 4: 'impl', 6: 'impl' }, agents: ROLES, ...extraArgs } })
const requested = (s, n) => s.events.filter(e => e === `task ${n}` || e.startsWith(`task ${n} `)).filter(e => !e.endsWith(' end'))
const fixLandAt = s => s.events.findIndex(e => e.startsWith('land ') && !e.endsWith(' end') && /testfix/.test(e))
const assertHeldUntilFixLands = (s, n) => {
  const first = s.events.findIndex(e => (e === `task ${n}` || e.startsWith(`task ${n} `)) && !e.endsWith(' end'))
  if (first < 0) return
  const landed = fixLandAt(s)
  assert.ok(landed >= 0 && first > landed, `task ${n} was requested at ${first}, fix landed at ${landed}: ${s.events.join(' | ')}`)
}

for (const [label, finding] of [
  ['file only', { file: FIX }],
  ['node id in test, file given', { file: FIX, test: `${FIX}::test_x` }],
  ['node id only', { test: `${FIX}::TestK::test_x[case]` }],
  ['bare function name with the file', { file: FIX, test: 'test_x' }],
  ['./ prefix', { file: `./${FIX}` }],
  ['upper-cased path', { file: 'Tests/Test_A.py' }],
]) {
  await test(`tc67_redteam workflow: finding naming ${label} keeps the verified task and its dependents waiting while an unrelated one runs`, async () => {
    const p = scenario(finding, { testFix: { [FIX]: {} } })
    await go(p)
    assertHeldUntilFixLands(p.state, '3')
    assertHeldUntilFixLands(p.state, '6')
    assert.equal(requested(p.state, '4').length > 0, true, 'unrelated Task 4 still runs')
  })
}

await test('tc67_redteam workflow: a finding naming tests of two test tasks holds both verified tasks, not the unrelated one', async () => {
  const p = project({
    deps: { 1: [], 2: [], 3: ['1'], 5: ['2'], 4: [] },
    durations: { 1: 5, 2: 5, 4: 5 },
    crossTest: { verdict: 'changes', findings: [blocking({ file: 'tests/test_a.py' }), blocking({ file: 'tests/test_b.py', test: 'tests/test_b.py::test_y' })], summary: 'drift' },
    testVerifies: { 'tests/test_a.py': ['3'], 'tests/test_b.py': ['5'] },
    testFix: { 'tests/test_a.py': {}, 'tests/test_b.py': {} },
  })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, wave: { 1: 1, 2: 1, 3: 2, 5: 2, 4: 1 }, tracks: { 1: 'test', 2: 'test', 3: 'impl', 5: 'impl', 4: 'impl' }, agents: ROLES } })
  assertHeldUntilFixLands(p.state, '3')
  assertHeldUntilFixLands(p.state, '5')
  assert.ok(requested(p.state, '4').length > 0)
})

await test('tc67_redteam workflow: a test-fix that fails its proof never lets the held task start', async () => {
  const p = scenario({ file: FIX }, { testFix: { [FIX]: { proof: { proved: false, reason: 'still fails' } } } })
  await go(p)
  assert.deepEqual(requested(p.state, '3'), [], p.state.events.join(' | '))
  assert.deepEqual(requested(p.state, '6'), [])
  assert.ok(requested(p.state, '4').length > 0)
})

await test('tc67_redteam workflow: a test-fix that is rejected by review never lets the held task start', async () => {
  const p = scenario({ file: FIX }, { testFix: { [FIX]: { review: { verdict: 'changes', findings: [{ severity: 'blocking', issue: 'weakened' }], summary: 'no' } } } })
  await go(p)
  assert.deepEqual(requested(p.state, '3'), [], p.state.events.join(' | '))
})

await test('tc67_redteam workflow: a test-fixer that cannot fix never lets the held task start', async () => {
  const p = scenario({ file: FIX }, { testFix: { [FIX]: { verdict: 'cannot-fix', finding: { file: FIX, test: 'test_x', issue: 'stuck' } } } })
  await go(p)
  assert.deepEqual(requested(p.state, '3'), [], p.state.events.join(' | '))
})

await test('tc67_redteam workflow: a test-fix whose land is red waits for the fix before the held task starts', async () => {
  const p = scenario({ file: FIX }, { testFix: { [FIX]: {} }, redLands: { testfix1: true } })
  await go(p)
  assertHeldUntilFixLands(p.state, '3')
})

await test('tc67_redteam workflow: a finding on a file that verifies nothing holds nothing', async () => {
  const p = scenario({ file: 'tests/test_other.py' }, { testFix: { 'tests/test_other.py': {} } })
  await go(p)
  assert.ok(requested(p.state, '3').length > 0, 'Task 3 runs: the finding names tests that verify no task')
})

await test('tc67_redteam workflow: no findings leaves every task free (7.5 side of the hold)', async () => {
  const p = project({ deps: { 1: [], 2: [], 3: ['1', '2'], 4: [] }, crossTest: { verdict: 'approve', findings: [], summary: 'ok' }, testVerifies: { [FIX]: ['3'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, wave: { 1: 1, 2: 1, 3: 2, 4: 1 }, tracks: { 1: 'test', 2: 'test', 3: 'impl', 4: 'impl' }, agents: ROLES } })
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'])
})
