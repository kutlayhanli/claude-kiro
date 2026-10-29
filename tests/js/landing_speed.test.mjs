// Landing-speed workflow examples, Stories 1-2 (script-side readiness, task-owned worktrees).
// Runs the real plugin/workflows/spec-implement.js under the fake runtime; agents are the only fakes.
// Assertions use observable behaviour only: requested labels and their order (trace), prompts,
// the result and the log. Run: node tests/js/landing_speed.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { project } from './fake_project.mjs'
import { loadDecisions } from './load_decisions.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')

const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
const WT = n => `/repo/.ck/worktrees/demo-task-${n}`
const REVIEW_ON = { reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 1 } }

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

// Args as the skill builds them: deps and remaining copied from `ck plan --json`.
const strDeps = deps => Object.fromEntries(Object.entries(deps).map(([k, v]) => [String(k), v.map(String)]))
const withGraph = (deps, extra = {}) => {
  const d = strDeps(deps)
  return { ...baseArgs, deps: d, remaining: Object.keys(d), ...extra }
}

// Trace helpers (trace: [{t, ev: 'request'|'start'|'end', label}]).
const requests = trace => trace.filter(e => e.ev === 'request').map(e => e.label)
const at = (trace, ev, label) => trace.findIndex(e => e.ev === ev && e.label === label)
const isPlan = l => /^plan \d+/.test(l)
const isPlanRetry = l => /^plan \d+.*\bretry\b/.test(l)
const planNo = l => Number(l.match(/^plan (\d+)/)[1])
// The first attempt of a task ("task 3", "task 3 [test]"); retries and revisions are not.
const firstTask = l => (String(l).match(/^task (\d+)(?: \[test\])?$/) || [])[1]
const landTasks = l => (l.startsWith('land ') ? l.slice(5).split(' ') : [])
const landEndOf = (trace, n) => trace.findIndex(e => e.ev === 'end' && landTasks(e.label).includes(n))
const idlePlans = logs => new Set(logs.map(m => (String(m).match(/^plan (\d+) \(idle\)/) || [])[1]).filter(Boolean).map(Number))
const sorted = a => [...a].map(String).sort((x, y) => Number(x) - Number(y))
const taskRec = (result, n) => (result.tasks || []).find(t => String(t.task) === n)
const mentions = (text, n) => new RegExp(`${WT(n).replace(/[.]/g, '\\.')}(?!\\d)`).test(text || '')
const createsTask = (text, n) => new RegExp(`worktree create demo ${n}(?![\\d\\w])`).test(text || '')

// 2 chains of 4 plus 4 independent tasks (TC-5, TC-12).
const TC5_DEPS = { 1: [], 2: ['1'], 3: ['2'], 4: ['3'], 5: [], 6: ['5'], 7: ['6'], 8: ['7'], 9: [], 10: [], 11: [], 12: [] }
const TC5_MERGE = Object.fromEntries(Object.keys(TC5_DEPS).map(n => [n, 10]))

await test('TC-2: the script schedules from args.deps: a dependent starts only after its dependency lands', async () => {
  const deps = { 1: [], 2: ['1'], 3: [] }
  const p = project({ deps })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  assert.deepEqual(sorted(result.merged), ['1', '2', '3'])
  const land1 = landEndOf(trace, '1')
  assert.ok(land1 >= 0, 'task 1 landed')
  assert.ok(at(trace, 'request', 'task 2') > land1, 'task 2 requested only after the landing of task 1 ended')
})

await test('TC-4: a freed implementing slot starts the next ready task without a plan agent (1.3)', async () => {
  const deps = { 1: [], 2: [], 3: [] }
  const p = project({ deps, durations: { 1: 5, 2: 50 }, reviewDurations: { 1: 200 } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2, agents: REVIEW_ON }) })
  assert.deepEqual(sorted(result.merged), ['1', '2', '3'])
  const reqs = requests(trace)
  const t3 = reqs.indexOf('task 3')
  assert.ok(t3 >= 0, 'task 3 was requested')
  const otherPlan = reqs.findIndex(l => isPlan(l) && l !== 'plan 1')
  const firstLand = reqs.findIndex(l => l.startsWith('land '))
  assert.ok(otherPlan < 0 || t3 < otherPlan, `task 3 requested before any plan other than plan 1: ${reqs.join(' | ')}`)
  assert.ok(firstLand < 0 || t3 < firstLand, `task 3 requested before any land: ${reqs.join(' | ')}`)
})

await test('TC-5: plan agents run only at start, after a landing, or when idle; fewer than one per task (1.4, NFR-1)', async () => {
  const p = project({ deps: TC5_DEPS, mergeDurations: TC5_MERGE })
  const { result, trace, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(TC5_DEPS, { maxConcurrent: 3, mergeBatch: 2 }) })
  assert.deepEqual(sorted(result.merged), Object.keys(TC5_DEPS))
  const idle = idlePlans(logs)
  let first = true
  let landEnded = false
  let plans = 0
  let landings = 0
  for (const e of trace) {
    if (e.ev === 'end' && e.label.startsWith('land ')) { landEnded = true; landings++ }
    if (e.ev !== 'request') continue
    if (isPlan(e.label)) {
      plans++
      if (!isPlanRetry(e.label)) {
        const k = planNo(e.label)
        if (!first && !idle.has(k)) {
          assert.ok(landEnded, `${e.label} is neither the first agent, the next plan after a landing, nor an idle plan`)
          landEnded = false
        }
      }
    }
    first = false
  }
  assert.ok(plans <= 1 + landings + idle.size, `plans ${plans} > 1 + landings ${landings} + idle ${idle.size}`)
  assert.ok(plans < 12, `${plans} plan agents for 12 tasks`)
  assert.equal(result.stats.planSteps, plans)
  for (const l of requests(trace)) {
    assert.ok(!/worktree|create/i.test(l), `no agent is a worktree-creation step: ${l}`)
    if (isPlan(l)) assert.ok(!/worktree create/.test(p.state.prompts[l]), `${l} prompt creates worktrees`)
  }
})

await test('TC-6: a dangling dependency keeps a task waiting; an idle plan picks up the relink mid-run (1.4c, 1.6, 1.10)', async () => {
  const deps = { 1: [], 2: ['7'] }
  const p = project({ deps, durations: { 1: 300 }, relink: { atPlan: 2, deps: { 2: [] } } })
  const { result, trace, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  const plan2Req = at(trace, 'request', 'plan 2')
  const plan2End = at(trace, 'end', 'plan 2')
  const t1End = at(trace, 'end', 'task 1')
  const t2 = at(trace, 'request', 'task 2')
  assert.ok(plan2Req >= 0 && plan2Req < t1End, 'an idle plan 2 was requested while task 1 ran')
  assert.ok(idlePlans(logs).has(2), `plan 2 is logged as the idle plan: ${logs.join(' | ')}`)
  assert.ok(t2 > plan2End, 'task 2 was not requested before plan 2 returned')
  assert.ok(t2 < t1End, 'task 2 was requested after plan 2 and before task 1 ended')
  assert.deepEqual(sorted(result.merged), ['1', '2'])
})

await test('TC-7: no plan storm while the run waits on a long task (1.5)', async () => {
  const deps = { 1: [], 2: ['1'] }
  const p = project({ deps, durations: { 1: 300 } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  const from = at(trace, 'request', 'plan 1')
  const to = at(trace, 'end', 'task 1')
  const between = trace.slice(from + 1, to).filter(e => e.ev === 'request' && isPlan(e.label))
  assert.ok(between.length <= 1, `plans while waiting on task 1: ${between.map(e => e.label).join(', ')}`)
  assert.deepEqual(sorted(result.merged), ['1', '2'])
})

await test('TC-8: a plan replaces the view: externally Done tasks are skipped, added tasks start (1.6)', async () => {
  // Deviation from the row's deps {3: []}: with 3 ready at the start, chain order would start it first.
  // 3 depends on 1 here and task 2 holds the only slot, so 3 can only become ready through plan 2 (after landing 1).
  const deps = { 1: [], 2: [], 3: ['1'], 4: ['3'] }
  let report = null
  const p = project({
    deps,
    durations: { 2: 100 },
    externalDone: { atPlan: 2, tasks: ['3'] },
    relink: { atPlan: 2, deps: { 5: [] } },
    gitReport: out => (report = out),
  })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 1 }) })
  const reqs = requests(trace)
  assert.ok(!reqs.includes('task 3'), 'task 3 (Done on the target) was never requested')
  assert.ok(reqs.includes('task 4'), 'task 4 started once 3 was Done')
  assert.ok(reqs.includes('task 5'), 'task 5, added mid-run, started')
  assert.equal(result.reportSource, 'git')
  assert.ok(report && report.done.some(e => e.task === '3'), 'the git report lists 3 as Done')
  assert.ok(!report.merged.some(e => e.task === '3'), 'and not as merged by the run')
  assert.ok(!p.state.landings.flat().includes('3'))
})

await test('TC-9: a dependency cycle at start refuses the run, as today (1.7)', async () => {
  const deps = { 1: ['2'], 2: ['1'] }
  const p = project({ deps, cycles: [['1', '2']] })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  assert.equal(result.halted, true)
  assert.equal(result.refused, true)
  assert.ok(!requests(trace).some(l => l.startsWith('task')))
})

const TC10 = { deps: { 1: [], 2: [], 3: [], 4: ['1'] }, durations: { 1: 5, 2: 50, 3: 50 } }
const VC4 = [{ task: '4', message: 'Task 4 is verified by Task 9, whose tests import runner (Task 5)' }]

await test('TC-10 (continue): a verify-order cycle found after landing 1 skips that task, others merge (1.7)', async () => {
  const p = project({ ...TC10, verifyCycles: VC4 })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(TC10.deps, { maxConcurrent: 2 }) })
  assert.ok(!requests(trace).some(l => /^task 4\b/.test(l)), 'task 4 never started')
  assert.ok(result.failed.some(f => f.task === '4' && f.reason.startsWith('verify-order cycle: ')), JSON.stringify(result.failed))
  assert.deepEqual(sorted(result.merged), ['1', '2', '3'])
})

await test('TC-10 (halt): no task starts after the plan that reports the cycle; running tasks still land (1.7)', async () => {
  const p = project({ ...TC10, verifyCycles: VC4 })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(TC10.deps, { maxConcurrent: 2, onFailure: 'halt' }) })
  const planEnd = at(trace, 'end', 'plan 2')
  assert.ok(planEnd >= 0, 'plan 2 ran')
  const late = trace.slice(planEnd).filter(e => e.ev === 'request' && /^task /.test(e.label))
  assert.deepEqual(late.map(e => e.label), [], 'no task requested after the plan that reported the cycle')
  const started = requests(trace).map(firstTask).filter(Boolean)
  assert.ok(!started.includes('4'))
  for (const n of started) assert.ok(result.merged.includes(n), `running task ${n} still landed`)
})

await test('TC-11: two failed plan attempts stop launching; running tasks still land (1.8)', async () => {
  const p = project({ ...TC10, planFail: [2] })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(TC10.deps, { maxConcurrent: 2 }) })
  const retry = at(trace, 'request', 'plan 2 retry')
  assert.ok(retry >= 0, 'plan 2 was retried')
  const late = trace.slice(retry).filter(e => e.ev === 'request' && /^task /.test(e.label))
  assert.deepEqual(late.map(e => e.label), [], 'no task requested after plan 2 retry')
  const started = requests(trace).map(firstTask).filter(Boolean)
  for (const n of started) assert.ok(result.merged.includes(n), `running task ${n} still landed`)
  assert.equal(result.reason, 'planning step failed')
})

await test('TC-12: without args.deps the run falls back to a plan agent per launch, and completes (1.9)', async () => {
  const p = project({ deps: TC5_DEPS, mergeDurations: TC5_MERGE })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 3, mergeBatch: 2 } })
  assert.deepEqual(sorted(result.merged), Object.keys(TC5_DEPS))
  // A launch burst: first-attempt task requests made together (nothing but their own 'start' between them).
  const bursts = []
  let cur = null
  let planSince = false
  for (const e of trace) {
    if (e.ev === 'start') continue
    const n = e.ev === 'request' && firstTask(e.label)
    if (n) {
      if (!cur) { cur = { tasks: [], planBefore: planSince }; bursts.push(cur); planSince = false }
      cur.tasks.push(n)
      continue
    }
    cur = null
    if (e.ev === 'end' && isPlan(e.label)) planSince = true
  }
  for (const b of bursts) assert.ok(b.planBefore, `tasks ${b.tasks.join(', ')} were requested with no plan answer since the previous task request`)
})

await test('TC-13: a dependency missing from the map never starts its dependent, and is not a planning failure (1.10, 1.8)', async () => {
  const deps = { 1: [], 2: ['99'], 3: [] }
  const p = project({ deps, done: ['99'] })  // ck plan lists 99 as done by mistake, so its ready includes 2
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  const reqs = requests(trace)
  assert.ok(!reqs.some(l => /^task 2\b/.test(l)), 'task 2 never requested')
  assert.ok(!reqs.some(isPlanRetry), `no plan retry: ${reqs.join(' | ')}`)
  assert.ok(result.merged.includes('1') && result.merged.includes('3'), JSON.stringify(result.merged))
  assert.notEqual(result.reason, 'planning step failed')
})

await test('TC-14: each task agent creates and claims its own worktree; plan agents create none (2.1)', async () => {
  const deps = { 1: [], 2: ['1'], 3: [] }
  const p = project({ deps })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { tracks: { 3: 'test' } }) })
  assert.deepEqual(sorted(result.merged), ['1', '2', '3'])
  const reqs = requests(trace)
  const firsts = reqs.filter(l => firstTask(l))
  assert.equal(firsts.length, 3)
  for (const l of firsts) {
    const n = firstTask(l)
    const lines = String(p.state.prompts[l]).split('\n')
    const has = cmd => lines.findIndex(line => new RegExp(`(^|\\s)${cmd.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\s|$)`).test(line))
    const create = has(`ck -C /repo worktree create demo ${n} --install --base integrate/demo --json`)
    const claim = has(`ck -C ${WT(n)} worktree claim demo ${n}`)
    assert.ok(create >= 0, `${l}: no create line`)
    assert.ok(claim > create, `${l}: the claim of ${WT(n)} must follow the create, on its own line`)
  }
  for (const l of reqs.filter(isPlan)) assert.ok(!/worktree create/.test(p.state.prompts[l]), `${l} creates a worktree`)
})

await test('TC-17: a worktree that already EXISTS is used like a CREATED one (2.2)', async () => {
  const deps = { 1: [], 2: [] }
  const p = project({ deps, worktree: { 1: { state: 'EXISTS', detail: 'feat/demo-task-1' } } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { agents: REVIEW_ON }) })
  assert.ok(requests(trace).includes('review 1'), 'task 1 was reviewed')
  assert.ok(result.merged.includes('1'))
  assert.equal(taskRec(result, '1').ok, true)
})

for (const onFailure of ['continue', 'halt']) {
  await test(`TC-18 (${onFailure}): a BUSY worktree is reported, never retried or touched, and does not halt (2.3)`, async () => {
    const deps = { 1: [], 2: [], 3: [] }
    const p = project({ deps, worktree: { 2: { state: 'BUSY', detail: 'claimed: ck task agent on hostA' } } })
    const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 1, onFailure, agents: REVIEW_ON }) })
    const t2 = taskRec(result, '2')
    assert.ok(t2, 'task 2 is in result.tasks')
    assert.equal(t2.ok, false)
    assert.equal(t2.reason, 'worktree busy: claimed: ck task agent on hostA')
    const reqs = requests(trace)
    const naming2 = reqs.filter(l => /^(task|review|resolve) 2\b/.test(l) || landTasks(l).includes('2') || /^fix .*\bTask 2\b/.test(l))
    assert.deepEqual(naming2, ['task 2'], 'exactly one agent ran for task 2')
    const after = p.state.events.slice(p.state.events.indexOf('task 2') + 1).filter(l => !/ end$/.test(l))
    for (const l of after) assert.ok(!mentions(p.state.prompts[l], 2), `${l} prompt names ${WT(2)}`)
    assert.ok(result.merged.includes('1') && result.merged.includes('3'), JSON.stringify(result.merged))
    assert.ok(p.state.events.indexOf('task 3') > p.state.events.indexOf('task 2 end'), 'task 3 still requested after task 2 was recorded')
  })
}

for (const onFailure of ['continue', 'halt']) {
  await test(`TC-20 (${onFailure}): a worktree creation ERROR fails the task with git's message and applies onFailure (2.4)`, async () => {
    const deps = { 1: [], 2: [], 3: [], 4: [], 5: [] }
    const detail = 'fatal: invalid reference: integrate/demo'
    const p = project({ deps, worktree: { 3: { state: 'ERROR', detail } } })
    const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 1, onFailure }) })
    const t3 = taskRec(result, '3')
    assert.ok(t3, 'task 3 is in result.tasks')
    assert.equal(t3.ok, false)
    assert.ok(String(t3.reason).includes(detail), t3.reason)
    assert.ok(!result.merged.includes('3'))
    const ev = p.state.events
    const after = ev.slice(ev.indexOf('task 3 end') + 1).filter(l => /^task /.test(l) && !/ end$/.test(l))
    if (onFailure === 'continue') {
      assert.ok(after.includes('task 4') && after.includes('task 5'), `other ready tasks keep starting: ${after.join(', ')}`)
    } else {
      assert.deepEqual(after, [], 'no task requested after task 3 was recorded')
    }
  })
}

await test('TC-21: retries, revisions and reviews use the existing worktree; none creates one (2.5)', async () => {
  const deps = { 1: [], 2: [] }
  const p = project({ deps, failUntil: { 1: 1 }, reviews: { 2: ['changes', 'approve'] } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { agents: REVIEW_ON }) })
  assert.deepEqual(sorted(result.merged), ['1', '2'])
  const reqs = requests(trace)
  const retry = reqs.find(l => /^task 1 retry\b/.test(l))
  assert.ok(retry, 'task 1 was retried')
  const rp = p.state.prompts[retry]
  assert.ok(mentions(rp, 1), 'the retry names the existing worktree')
  assert.ok(rp.includes(`ck -C ${WT(1)} worktree claim demo 1 --takeover`), 'the retry takes over the existing worktree')
  if (createsTask(rp, 1)) {
    const segments = rp.split(/\n|\.\s/).filter(s => createsTask(s, 1))
    for (const s of segments) assert.ok(/MISSING/.test(s), `the retry creates the worktree only when the claim reports MISSING: ${s}`)
  }
  const others = [
    [1, reqs.filter(l => /^review 1\b/.test(l))],
    [2, reqs.filter(l => /^review 2\b/.test(l) || /^task 2 revise\b/.test(l))],
  ]
  assert.ok(others[1][1].some(l => /revise/.test(l)), 'task 2 was revised')
  assert.ok(others[1][1].filter(l => /^review 2/.test(l)).length >= 2, 'task 2 was reviewed twice')
  for (const [n, labels] of others) {
    assert.ok(labels.length, `agents for task ${n}`)
    for (const l of labels) {
      assert.ok(mentions(p.state.prompts[l], n), `${l} names ${WT(n)}`)
      assert.ok(!createsTask(p.state.prompts[l], n), `${l} creates a worktree`)
    }
  }
})

await test('TC-22: a fixer after a red gate still creates its own fix worktree, as today (2.6)', async () => {
  // Deviation from the row's checkRed: ['2']: today the `fix 1 …` agent of a task red on landing works in the
  // task worktree (landFixPrompt) and never creates fix1. The fixer worktree is created after a red target gate,
  // so that case carries the fix1 assertion; the red-landing fixer is checked to be unchanged too.
  const deps = { 1: [], 2: [] }
  const p = project({ deps, fullRed: true })
  const { trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { fullGateEvery: 1, mergeBatch: 1 }) })
  const fix = requests(trace).find(l => /^fix 1 /.test(l))
  assert.ok(fix, 'a fixer ran after the red full gate')
  assert.ok(p.state.prompts[fix].includes('worktree create demo fix1 --install --base integrate/demo'), p.state.prompts[fix])

  const q = project({ deps, checkRed: ['2'] })
  const r2 = await runWorkflow(SCRIPT, { agent: q.agent, args: withGraph(deps) })
  const landFix = requests(r2.trace).find(l => /^fix 1 \(Task 2 red on landing\)/.test(l))
  assert.ok(landFix, 'the red landing of task 2 got a fixer')
  assert.ok(mentions(q.state.prompts[landFix], 2), 'it works in the task worktree, as today')
  assert.ok(!createsTask(q.state.prompts[landFix], 2))
  assert.ok(r2.result.merged.includes('2'))
})

await test('TC-55 (a): the script and readyTasks agree that a dangling dependency is never met (1.10)', async () => {
  const deps = { 1: [], 2: ['7'] }
  const p = project({ deps, durations: { 1: 30 } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  assert.ok(!requests(trace).some(l => /^task 2\b/.test(l)), 'the script never starts task 2')
  assert.ok(result.merged.includes('1'))
  const { readyTasks } = loadDecisions(SCRIPT)
  const d = strDeps(deps)
  for (const done of [[], ['1'], ['1', '7']]) {
    for (const exclude of [[], ['1']]) {
      const ready = readyTasks({ order: ['1', '2'], deps: d, done, exclude, blocked: [] })
      assert.ok(!ready.includes('2'), `readyTasks(done=${done}, exclude=${exclude}) = ${ready}`)
    }
  }
})

await test('TC-55 (b): first task requests follow the heads of readyTasks, critical path first (1.2)', async () => {
  const deps = { 1: [], 2: [], 3: ['2'], 4: ['3'], 10: [] }
  const p = project({ deps, mergeDurations: { 2: 50 } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 1 }) })
  assert.deepEqual(sorted(result.merged), ['1', '2', '3', '4', '10'])
  const { readyTasks } = loadDecisions(SCRIPT)
  const d = strDeps(deps)
  const order = Object.keys(d)
  const requested = []
  const done = new Set()
  for (const e of trace) {
    if (e.ev === 'end' && e.label.startsWith('land ')) for (const n of landTasks(e.label)) done.add(n)  // the fake merges every landed task here
    const n = e.ev === 'request' && firstTask(e.label)
    if (!n) continue
    const head = readyTasks({ order, deps: d, done: [...done], exclude: [...requested], blocked: [] })[0]
    assert.equal(n, head, `request #${requested.length + 1} (task ${n}) vs readyTasks head ${head}`)
    requested.push(n)
  }
  assert.deepEqual(requested.slice(0, 3), ['2', '1', '10'], 'chain order, not numeric order')
})

await test('TC-62: a plan is checked against the exclude list of its own prompt, so starts during it are no failure (1.6, 1.8)', async () => {
  const deps = { 1: [], 2: [], 3: [], 4: [] }
  const p = project({ deps, durations: { 1: 5, 2: 30 }, planDurations: { 2: 60 } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2, mergeBatch: 1 }) })
  const from = at(trace, 'request', 'plan 2')
  const to = at(trace, 'end', 'plan 2')
  assert.ok(from >= 0 && to > from, 'plan 2 ran')
  assert.ok(trace.slice(from, to).some(e => e.ev === 'request' && firstTask(e.label)), 'a task was requested while plan 2 ran')
  assert.ok(!requests(trace).includes('plan 2 retry'), 'plan 2 was not retried')
  assert.notEqual(result.reason, 'planning step failed')
  assert.deepEqual(sorted(result.merged), ['1', '2', '3', '4'])
})
