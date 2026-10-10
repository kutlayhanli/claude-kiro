// Scheduler tests for spec_implement.js with stub agents. Run: node tests/js/spec_implement.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, sleep } from './load_workflow.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../src/claude_kiro/resources/templates/workflows/spec_implement.js')

const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.claude/worktrees/demo-integration' }

// A fake project: `ck plan` semantics over deps/done, task durations, failures, timestamps.
function project({ deps, done = [], durations = {}, failUntil = {}, noCommitsFirst = [], busy = [], cycles = [], verifyCycles = [], verifyCyclesFromStart = false, checkRed = [], fullRed = false, relink = null, reviews = {}, mergeDurations = {}, reviewDurations = {}, recheckRed = 0, conflicts = [], redLands = {}, pairConflicts = [] }) {
  let rechecksLeftRed = recheckRed
  // RED landings left per task: checkRed tasks are red on their first landing, redLands sets a count.
  const redLeft = { ...Object.fromEntries(checkRed.map(n => [n, 1])), ...redLands }
  const state = { deps: { ...deps }, done: new Set(done), events: [], prompts: {}, opts: {}, reviewCount: {}, attempts: {}, plans: 0, merging: 0, maxMerging: 0, running: 0, maxRunning: 0, landings: [], resolved: new Set() }
  let clock = Date.parse('2026-10-04T10:00:00Z')
  const stamp = () => new Date((clock += 60000)).toISOString().replace('.000', '')
  const ready = exclude =>
    Object.keys(state.deps).filter(n => !state.done.has(n) && !exclude.includes(n) && !verifyCycles.some(v => v.task === n) && state.deps[n].every(d => state.done.has(d)))
  const agent = async (prompt, opts) => {
    const l = opts.label
    state.events.push(l)
    state.prompts[l] = prompt
    state.opts[l] = opts
    if (l.startsWith('plan')) {
      state.plans++
      if (relink && state.plans === relink.atPlan) Object.assign(state.deps, relink.deps)
      const exclude = ((prompt.match(/--exclude ([\d,]+)/) || [])[1] || '').split(',').filter(Boolean)
      const slotsMatch = prompt.match(/Take the first (\d+|all) task/)
      let r = ready(exclude)
      if (slotsMatch && slotsMatch[1] !== 'all') r = r.slice(0, Number(slotsMatch[1]))
      return {
        done: [...state.done],
        ready: r.filter(n => !busy.includes(n)),
        busy: r.filter(n => busy.includes(n)).map(n => ({ task: n, detail: 'claimed: someone' })),
        remaining: Object.keys(state.deps).filter(n => !state.done.has(n)),
        cycles,
        verifyCycles: state.plans === 1 && !verifyCyclesFromStart ? [] : verifyCycles,
      }
    }
    if (l.startsWith('task')) {
      const n = l.match(/task (\d+)/)[1]
      state.attempts[n] = (state.attempts[n] || 0) + 1
      state.running++
      state.maxRunning = Math.max(state.maxRunning, state.running)
      const startedAt = stamp()
      await sleep(durations[n] ?? 5)
      state.running--
      state.events.push(`${l} end`)
      if (noCommitsFirst.includes(n) && state.attempts[n] === 1) {
        return { task: n, status: 'blocked', gatePassed: false, summary: 'answered the wave question', commits: '', blocker: 'user asked a question', startedAt, finishedAt: stamp() }
      }
      const pass = state.attempts[n] > (failUntil[n] || 0)
      return { task: n, status: pass ? 'done' : 'in_progress', gatePassed: pass, summary: `attempt ${state.attempts[n]}`, commits: 'abc123', startedAt, finishedAt: stamp() }
    }
    if (l.startsWith('land ')) {
      // `ck worktree land`: precheck (conflicts, pairwise deferral), one gate, bisect a red batch.
      const tasks = l.slice(5).split(' ')
      state.landings.push(tasks)
      state.merging++
      state.maxMerging = Math.max(state.maxMerging, state.merging)
      const startedAt = stamp()
      await sleep(Math.max(...tasks.map(n => mergeDurations[n] ?? 2)))
      state.merging--
      state.events.push(`${l} end`)
      const out = []
      const landing = []
      for (const n of tasks) {
        if (conflicts.includes(n) && !state.resolved.has(n)) { out.push({ task: n, state: 'CONFLICT', files: ['specs/demo/tasks.md'] }); continue }
        const mate = pairConflicts.find(([a, b]) => b === n && landing.includes(a))
        if (mate) { out.push({ task: n, state: 'DEFERRED', detail: `conflicts with Task ${mate[0]} in this batch`, files: ['x.py'] }); continue }
        landing.push(n)
      }
      const red = landing.filter(n => (redLeft[n] || 0) > 0)
      const gates = landing.length ? [{ tasks: landing, green: !red.length }] : []
      if (red.length && landing.length > 1) gates.push({ tasks: landing.slice(0, 1), green: true })
      for (const n of landing) {
        if (red.includes(n)) { redLeft[n]--; out.push({ task: n, state: 'RED', checkGreen: false, checkTail: `red: task ${n}` }) }
        else { state.done.add(n); out.push({ task: n, state: 'MERGED', checkGreen: true, mergedAt: stamp() }) }
      }
      return { tasks: out, gates, bisected: gates.length > 1, startedAt, finishedAt: stamp() }
    }
    if (l.startsWith('resolve')) {
      const n = l.match(/resolve (\d+)/)[1]
      state.resolved.add(n)
      return { resolved: true, conflicts: 'specs/demo/tasks.md: kept both task sections' }
    }
    if (l.startsWith('review')) {
      const n = l.match(/review (\d+)/)[1]
      state.reviewCount[n] = (state.reviewCount[n] || 0) + 1
      await sleep(reviewDurations[n] ?? 0)
      state.events.push(`${l} end`)
      const verdict = (reviews[n] || [])[state.reviewCount[n] - 1] || 'approve'
      return { verdict, findings: verdict === 'changes' ? [{ severity: 'blocking', issue: 'contradicts design.md' }] : [], summary: verdict }
    }
    if (l.startsWith('fix')) return { green: true }
    if (l.startsWith('re-check')) return { green: rechecksLeftRed-- <= 0 }
    if (l.startsWith('full gate')) return { green: !fullRed }
    if (l === 'final gate') return { green: true }
    throw new Error('unexpected agent ' + l)
  }
  return { agent, state }
}

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}
const idx = (s, l) => s.events.indexOf(l)
// Label of the first landing that included task n (or its end event).
const landOf = (s, n, end = false) => s.events.find(e => e.startsWith('land ') && e.endsWith(' end') === end && e.replace(/ end$/, '').slice(5).split(' ').includes(n))

await test('no wave barrier: a task starts as soon as its own dependencies merge', async () => {
  // 1 -> {2 (fast), 3 (slow)}; 4 needs only 2. Waves would hold 4 until 3 finished.
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'], 4: ['2'] }, durations: { 1: 2, 2: 5, 3: 80, 4: 5 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, wave: { 1: 1, 2: 2, 3: 2, 4: 3 } } })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'])
  assert.ok(idx(p.state, 'task 4') < idx(p.state, 'task 3 end'), 'task 4 started while task 3 was still running')
  assert.equal(p.state.maxMerging, 1, 'merges are serialized')
  assert.ok(p.state.events.includes('final gate'))
})

await test('already-Done tasks are skipped', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, done: ['1'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(!p.state.events.includes('task 1'))
  assert.deepEqual(result.merged, ['2'])
  assert.equal(result.stats.initiallyDone, 1)
})

await test('refuses to launch on a dependency cycle or verify-order cycle', async () => {
  const p = project({ deps: { 1: ['2'], 2: ['1'] }, cycles: [['1', '2', '1']] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.equal(result.refused, true)
  assert.ok(!p.state.events.some(e => e.startsWith('task')))

  const q = project({ deps: { 1: [], 2: [] }, verifyCycles: [{ task: '1', message: 'Task 1 is verified by Task 9 ...' }], verifyCyclesFromStart: true })
  const r2 = await runWorkflow(SCRIPT, { agent: q.agent, args: baseArgs })
  assert.equal(r2.result.refused, true)
  assert.deepEqual(r2.result.verifyCycles.map(v => v.task), ['1'])
  assert.ok(!q.state.events.some(e => e.startsWith('task')), 'nothing starts, not even the unaffected task 2')
})

await test('a verify-order cycle found mid-run skips that task and keeps going', async () => {
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'] }, verifyCycles: [{ task: '3', message: 'Task 3 is verified by Task 9, whose tests import runner (Task 4)' }] })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(!p.state.events.includes('task 3'))
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(result.failed.some(f => f.task === '3' && f.reason.startsWith('verify-order cycle')))
  assert.ok(logs.some(l => l.includes('verify-order cycle')))
})

await test('relinks in tasks.md apply without a restart', async () => {
  // 3 spuriously depends on 2 (slow); a relink after the first task removes the edge.
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['2'] }, durations: { 2: 80 }, relink: { atPlan: 2, deps: { 3: ['1'] } } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(idx(p.state, 'task 3') < idx(p.state, 'task 2 end'), 'task 3 started before task 2 finished, after the relink')
})

await test('continue (default): dependents of a failed task never start, others do', async () => {
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'], 4: ['3'], 5: ['2'] }, failUntil: { 3: 99 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxRetries: 1 } })
  assert.equal(p.state.attempts['3'], 2)
  assert.ok(p.state.events.includes('task 5'))
  assert.ok(!p.state.events.includes('task 4'))
  assert.deepEqual(result.notStarted, ['4'])
  assert.deepEqual(result.failed.map(f => f.task), ['3'])
  assert.equal(result.targetGreen, true)
})

await test('halt: a failure stops new launches, running tasks still finish and merge', async () => {
  const p = project({ deps: { 1: [], 2: ['1'], 3: ['1'], 4: ['2'] }, failUntil: { 3: 99 }, durations: { 2: 30, 3: 5 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, onFailure: 'halt', maxRetries: 0 } })
  assert.equal(result.halted, true)
  assert.ok(landOf(p.state, '2'), 'task 2 was running and still merged')
  assert.ok(!p.state.events.includes('task 4'))
  assert.match(result.reason, /Task 3 failed/)
})

await test('an attempt with no commits (answered a chat message) is retried with the mandate', async () => {
  const p = project({ deps: { 1: [] }, noCommitsFirst: ['1'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.deepEqual(result.merged, ['1'])
  assert.match(p.state.prompts['task 1'], /^MANDATE: The user ran \/spec:implement demo/)
  assert.match(p.state.prompts['task 1 retry 1'], /made NO commits/)
  assert.match(p.state.prompts['task 1 retry 1'], /^MANDATE/)
})

await test('per-landing check: a task red on landing stays off the target, gets one fix, then lands', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, checkRed: ['1'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(p.state.events.includes('fix 1 (Task 1 red on landing)'))
  assert.match(p.state.prompts['fix 1 (Task 1 red on landing)'], /red: task 1/)
  assert.match(p.state.prompts['fix 1 (Task 1 red on landing)'], /Work only in the task worktree \/repo\/\.claude\/worktrees\/demo-task-1/)
  assert.deepEqual(p.state.landings, [['1'], ['1'], ['2']])
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.equal(result.tasks.find(t => t.task === '1').fixedAfterMerge, true)
  assert.equal(result.targetGreen, true)
})

const QUEUED_FULL = { deps: { 1: [], 2: [], 3: [], 4: [] }, durations: { 1: 2, 2: 10, 3: 10, 4: 10 }, mergeDurations: { 1: 60 } }

await test('full gate every N merges; red full gate after a failed fix halts', async () => {
  const p = project({ deps: { 1: [], 2: [], 3: [], 4: ['1'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, fullGateEvery: 2, mergeBatch: 1 } })
  assert.deepEqual(result.fullGates.map(g => g.afterMerges), [2, 4])
  // With batches, the full gate runs after the landing that crosses each multiple of N.
  const b = project({ ...QUEUED_FULL })
  const rb = await runWorkflow(SCRIPT, { agent: b.agent, args: { ...baseArgs, fullGateEvery: 2 } })
  assert.deepEqual(b.state.landings, [['1'], ['2', '3', '4']])
  assert.deepEqual(rb.result.fullGates.map(g => g.afterMerges), [4])

  const q = project({ deps: { 1: [], 2: [], 3: ['1', '2'] }, fullRed: true })
  const agent = async (prompt, opts) => (opts.label.startsWith('re-check') ? { green: false } : q.agent(prompt, opts))
  const r2 = await runWorkflow(SCRIPT, { agent, args: { ...baseArgs, fullGateEvery: 2 } })
  assert.equal(r2.result.halted, true)
  assert.equal(r2.result.targetGreen, false)
  assert.ok(!q.state.events.includes('task 3'))
})

await test('busy worktree is reported, not run', async () => {
  const p = project({ deps: { 1: [], 2: [] }, busy: ['2'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(!p.state.events.includes('task 2'))
  assert.ok(result.failed.some(f => f.task === '2' && f.reason.startsWith('worktree busy')))
})

await test('maxConcurrent caps task agents at once', async () => {
  const p = project({ deps: { 1: [], 2: [], 3: [], 4: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 2 } })
  assert.equal(p.state.maxRunning, 2)
})

const REVIEW_ON = { reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 1 } }

await test('a task in review or waiting to merge does not hold a slot', async () => {
  // One slot. Task 1's merge is slow and task 2's review is slow; neither should keep the next task waiting.
  const p = project({ deps: { 1: [], 2: [], 3: [] }, durations: { 1: 2, 2: 2, 3: 2 }, mergeDurations: { 1: 80 }, reviewDurations: { 2: 80 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 1, agents: REVIEW_ON } })
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'])
  assert.equal(p.state.maxRunning, 1, 'still one implementing agent at a time')
  assert.ok(idx(p.state, 'task 2') < idx(p.state, landOf(p.state, '1', true)), 'task 2 started while task 1 waited to merge')
  assert.ok(idx(p.state, 'task 3') < idx(p.state, 'review 2 end'), 'task 3 started while task 2 was in review')
})

await test('a revision after review still runs when the slots are full', async () => {
  const p = project({ deps: { 1: [], 2: [] }, durations: { 1: 2, 2: 40 }, reviews: { 1: ['changes', 'approve'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 1, agents: REVIEW_ON } })
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(idx(p.state, 'task 1 revise 1') > -1, 'task 1 was revised')
  assert.ok(idx(p.state, 'task 1 revise 1') < idx(p.state, 'task 2 end'), 'the revision did not wait for task 2')
})

await test('wall-clock timing per task and per wave group', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, wave: { 1: 1, 2: 2 } } })
  const t1 = result.tasks.find(t => t.task === '1')
  assert.ok(t1.agentMinutes > 0 && t1.toMergedMinutes >= t1.agentMinutes)
  assert.deepEqual(result.timing.phases.map(ph => ph.group).sort(), ['Wave 1', 'Wave 2'])
  assert.ok(result.timing.minutes > 0)
})

await test('model and effort: unset means task agents inherit the session', async () => {
  const p = project({ deps: { 1: [] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  const o = p.state.opts['task 1']
  assert.equal('model' in o, false)
  assert.equal('effort' in o, false)
  assert.deepEqual(result.taskAgents, { model: 'inherited', effort: 'inherited' })
})

await test('model and effort: applied to task, retry and fix agents only', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 1 }, checkRed: ['1'] })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, model: 'sonnet', effort: 'medium' } })
  for (const l of ['task 1', 'task 1 retry 1', 'fix 1 (Task 1 red on landing)']) {
    assert.equal(p.state.opts[l].model, 'sonnet', l)
    assert.equal(p.state.opts[l].effort, 'medium', l)
  }
  for (const l of ['plan 1', 'land 1']) {
    assert.equal('model' in p.state.opts[l], false, l)
    assert.equal(p.state.opts[l].effort, 'low', l)
  }
  assert.equal('model' in p.state.opts['final gate'], false)
  assert.deepEqual(result.taskAgents, { model: 'sonnet', effort: 'medium' })
  assert.ok(logs.some(m => m.includes('impl sonnet/medium')))
})

await test('model and effort: unknown values are refused before any agent runs', async () => {
  for (const bad of [{ effort: 'fast' }, { model: 'gpt-5' }]) {
    const p = project({ deps: { 1: [] } })
    await assert.rejects(runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, ...bad } }), /unknown (effort|model)/)
    assert.equal(p.state.events.length, 0)
  }
})

await test('every gate-running prompt tells the agent to wait for the real exit code', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 1 }, checkRed: ['1'] })
  await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  for (const l of ['task 1', 'task 1 retry 1', 'land 1', 'fix 1 (Task 1 red on landing)', 'final gate']) {
    assert.ok(p.state.prompts[l], `no prompt for ${l}`)
    assert.match(p.state.prompts[l], /A slow gate is not a failed gate/, l)
  }
  assert.doesNotMatch(p.state.prompts['plan 1'], /slow gate/)
})

const ROLES = {
  implementer: { model: 'sonnet', effort: 'medium' },
  test_writer: { model: 'opus', effort: 'high' },
  reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 1 },
  fixer: { model: 'sonnet', effort: 'medium' },
  orchestrator: { model: 'sonnet', effort: 'low' },
  resolver: { model: 'opus', effort: 'high' },
}

await test('roles: impl tasks use the implementer, test tasks the test writer, fixes the fixer', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, checkRed: ['2'] })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, tracks: { 1: 'test', 2: 'impl' }, agents: ROLES } })
  assert.deepEqual([p.state.opts['task 1 [test]'].model, p.state.opts['task 1 [test]'].effort], ['opus', 'high'])
  assert.deepEqual([p.state.opts['task 2'].model, p.state.opts['task 2'].effort], ['sonnet', 'medium'])
  assert.deepEqual([p.state.opts['fix 1 (Task 2 red on landing)'].model, p.state.opts['fix 1 (Task 2 red on landing)'].effort], ['sonnet', 'medium'])
})

await test('reviewer: approves, then the task merges; review runs at the reviewer settings', async () => {
  const p = project({ deps: { 1: [] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: ROLES } })
  assert.deepEqual(result.merged, ['1'])
  assert.deepEqual([p.state.opts['review 1'].model, p.state.opts['review 1'].effort], ['opus', 'high'])
  assert.ok(idx(p.state, 'review 1') < idx(p.state, 'land 1'))
  assert.equal(result.tasks[0].review.verdict, 'approve')
  assert.match(p.state.prompts['review 1'], /diff integrate\/demo\.\.\.feat\/demo-task-1/)
})

await test('reviewer: blocking findings send the task back once, then it merges', async () => {
  const p = project({ deps: { 1: [] }, reviews: { 1: ['changes', 'approve'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: ROLES } })
  assert.deepEqual(result.merged, ['1'])
  assert.ok(p.state.events.includes('task 1 revise 1'))
  assert.match(p.state.prompts['task 1 revise 1'], /contradicts design\.md/)
  assert.equal(p.state.opts['task 1 revise 1'].model, 'sonnet')
  assert.ok(idx(p.state, 'review 1 round 2') < idx(p.state, 'land 1'))
  assert.deepEqual([result.tasks[0].review.verdict, result.tasks[0].review.rounds], ['approve', 1])
})

await test('reviewer: findings that survive the revisions fail the task and nothing merges', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, reviews: { 1: ['changes', 'changes'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: ROLES } })
  assert.deepEqual(result.merged, [])
  assert.match(result.failed[0].reason, /review: blocking findings remain after 1 revision/)
  assert.ok(!landOf(p.state, '1'))
  assert.ok(!p.state.events.includes('task 2'))  // its dependent never starts
})

await test('reviewer: enabled false skips review', async () => {
  const p = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: { ...ROLES, reviewer: { ...ROLES.reviewer, enabled: false } } } })
  assert.ok(!p.state.events.some(e => e.startsWith('review')))
})

await test('without agents settings every agent inherits and nothing is reviewed', async () => {
  const p = project({ deps: { 1: [] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.ok(!p.state.events.some(e => e.startsWith('review')))
  assert.equal('model' in p.state.opts['task 1'], false)
  assert.equal(result.agents.reviewer, 'off')
})

await test('roles: inherit means the session model; model/effort args override code-writing roles', async () => {
  const p = project({ deps: { 1: [] } })
  const roles = { ...ROLES, implementer: { model: 'inherit', effort: 'inherit' } }
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: roles } })
  assert.equal('model' in p.state.opts['task 1'], false)
  const q = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: q.agent, args: { ...baseArgs, agents: ROLES, model: 'haiku', effort: 'low' } })
  assert.deepEqual([q.state.opts['task 1'].model, q.state.opts['task 1'].effort], ['haiku', 'low'])
  assert.equal(q.state.opts['review 1'].model, 'opus')  // the override does not touch the reviewer
})

// --- escalation to bigger models ---------------------------------------------

const HAIKU = (escalate, extra = {}) => ({
  implementer: { model: 'haiku', effort: 'medium', escalate },
  test_writer: { model: 'haiku', effort: 'medium', escalate },
  fixer: { model: 'haiku', effort: 'medium', escalate },
  reviewer: { model: 'haiku', effort: 'medium', enabled: true, rounds: 1 },
  ...extra,
})
const modelsOf = (s, prefix) => s.events.filter(e => e.startsWith(prefix) && !e.endsWith(' end')).map(e => s.opts[e].model)

await test('escalation: retries climb the ladder after the normal retries fail, at the role effort', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 3 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxRetries: 1, agents: HAIKU(['sonnet', 'opus']) } })
  assert.deepEqual(modelsOf(p.state, 'task 1'), ['haiku', 'haiku', 'sonnet', 'opus'])
  assert.ok(p.state.events.filter(e => e.startsWith('task 1') && !e.endsWith(' end')).every(e => p.state.opts[e].effort === 'medium'))
  assert.deepEqual(result.merged, ['1'])
  assert.deepEqual(result.tasks[0].escalations, [{ stage: 'retry', model: 'sonnet' }, { stage: 'retry', model: 'opus' }])
  assert.deepEqual(result.escalated, [{ task: '1', models: ['sonnet', 'opus'], ok: true }])
})

await test('escalation: stops at the first model that passes', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 2 } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxRetries: 1, agents: HAIKU(['sonnet', 'opus']) } })
  assert.deepEqual(modelsOf(p.state, 'task 1'), ['haiku', 'haiku', 'sonnet'])
})

await test('escalation: off by default, so a failing task just fails', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 5 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxRetries: 1, agents: HAIKU([]) } })
  assert.deepEqual(modelsOf(p.state, 'task 1'), ['haiku', 'haiku'])
  assert.deepEqual(result.escalated, [])
  assert.equal(result.failed[0].task, '1')
})

await test('escalation: blocking review findings that survive the rounds get a revision on the next model', async () => {
  const p = project({ deps: { 1: [] }, reviews: { 1: ['changes', 'changes', 'approve'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: HAIKU(['opus']) } })
  assert.deepEqual(modelsOf(p.state, 'task 1 revise'), ['haiku', 'opus'])
  assert.deepEqual(result.merged, ['1'])
  assert.deepEqual(result.tasks[0].escalations, [{ stage: 'revise', model: 'opus' }])
  assert.equal(result.tasks[0].review.verdict, 'approve')
})

await test('escalation: a task still red on landing after the fix gets a fix on the next model', async () => {
  const p = project({ deps: { 1: [] }, redLands: { 1: 2 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: HAIKU(['opus'], { reviewer: { enabled: false } }) } })
  assert.deepEqual(modelsOf(p.state, 'fix'), ['haiku', 'opus'])
  assert.equal(result.targetGreen, true)
  assert.deepEqual(result.merged, ['1'])
  assert.deepEqual(result.fixerEscalations, [{ what: 'landing Task 1', model: 'opus', green: true }])
})

await test('escalation: a red full gate the fixer could not repair gets a fix on the next model', async () => {
  const p = project({ deps: { 1: [] }, fullRed: true, recheckRed: 1 })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, fullGateEvery: 1, agents: HAIKU(['opus'], { reviewer: { enabled: false } }) } })
  assert.deepEqual(modelsOf(p.state, 'fix'), ['haiku', 'opus'])
  assert.equal(result.targetGreen, true)
  assert.deepEqual(result.fixerEscalations, [{ what: 'full gate after 1 merges', model: 'opus', green: true }])
})

await test('handoff: agents leave a note on failure and every retry or revision reads it first', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 2 }, reviews: { 1: ['changes', 'approve'] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxRetries: 1, agents: HAIKU(['sonnet']) } })
  const note = '/repo/.claude/worktrees/demo-task-1.handoff.md'
  assert.match(p.state.prompts['task 1'], new RegExp(`write a handoff note to ${note}`))
  for (const l of ['task 1 retry 1', 'task 1 retry 2 (sonnet)', 'task 1 revise 1']) {
    assert.match(p.state.prompts[l], new RegExp(`read ${note}`), l)
    assert.match(p.state.prompts[l], new RegExp(`write a handoff note to ${note}`), l)
  }
})

await test('agents run ck with -C <dir>, never cd', async () => {
  const p = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  for (const l of ['plan 1', 'task 1', 'land 1']) {
    assert.match(p.state.prompts[l], /ck -C /, l)
    assert.doesNotMatch(p.state.prompts[l], /\bcd \/[^ ]+ &&/, l)  // a real cd into a path, not the rule's own wording
  }
})

await test('escalation: a role never escalates to a model that is not above its own', async () => {
  // The fixer is Opus and follows the implementer's ladder (sonnet > opus): nothing is above Opus, so no fixer escalation.
  const p = project({ deps: { 1: [] }, redLands: { 1: 5 } })
  const agents = { ...HAIKU(['sonnet', 'opus'], { reviewer: { enabled: false } }), fixer: { model: 'opus', effort: 'medium', escalate: null } }
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents } })
  assert.deepEqual(modelsOf(p.state, 'fix'), ['opus'])
  assert.deepEqual(result.fixerEscalations, [])
  assert.match(result.failed[0].reason, /red on landing after 1 fix/)
  assert.equal(result.targetGreen, true, 'the red task never landed')
})

await test('orchestrator: plan, landing, gate and re-check steps use the orchestrator role', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, fullRed: true })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, fullGateEvery: 1, agents: ROLES } })
  for (const l of ['plan 1', 'land 1', 'full gate @1', 're-check 1']) {
    assert.deepEqual([p.state.opts[l].model, p.state.opts[l].effort], ['sonnet', 'low'], l)
  }
  assert.equal(result.agents.orchestrator, 'sonnet/low')
})

await test('orchestrator: without agents settings plan and merge stay at low effort on the session model', async () => {
  const p = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.deepEqual([p.state.opts['plan 1'].model, p.state.opts['plan 1'].effort], [undefined, 'low'])
  assert.deepEqual([p.state.opts['land 1'].model, p.state.opts['land 1'].effort], [undefined, 'low'])
  assert.equal('model' in p.state.opts['final gate'], false)
})

await test('resolver: a conflict goes to the resolver, then the task lands again', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] }, conflicts: ['2'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: ROLES } })
  assert.ok(idx(p.state, 'land 2') < idx(p.state, 'resolve 2'))
  assert.deepEqual(p.state.landings, [['1'], ['2'], ['2']])
  assert.match(p.state.prompts['resolve 2'], /Do NOT merge into integrate\/demo yourself/)
  assert.deepEqual([p.state.opts['resolve 2'].model, p.state.opts['resolve 2'].effort], ['opus', 'high'])
  assert.match(p.state.prompts['resolve 2'], /specs\/demo\/tasks\.md/)
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(!p.state.events.includes('resolve 1'), 'no resolver without a conflict')
})

// --- risk and complexity routing ----------------------------------------------

const TIERED = (extra = {}) => ({
  implementer: { model: 'haiku', effort: 'medium', escalate: ['sonnet', 'opus'], risky_model: null, complexity_routing: false, ...(extra.implementer || {}) },
  test_writer: { model: 'haiku', effort: 'medium', escalate: ['sonnet', 'opus'] },
  fixer: { model: 'opus', effort: 'medium', escalate: ['sonnet', 'opus'] },
  reviewer: { model: 'sonnet', effort: 'medium', enabled: true, rounds: 1, risky_model: 'opus', risky_effort: 'high', ...(extra.reviewer || {}) },
  orchestrator: { model: 'sonnet', effort: 'low' },
  resolver: { model: 'opus', effort: 'high' },
})

await test('risk: a safety task starts at the top of the ladder and gets the risky reviewer', async () => {
  const p = project({ deps: { 1: [], 2: [] } })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: TIERED(), risk: { 1: 'safety', 2: 'normal' } } })
  assert.deepEqual([p.state.opts['task 1'].model, p.state.opts['task 1'].effort], ['opus', 'medium'])
  assert.deepEqual([p.state.opts['review 1'].model, p.state.opts['review 1'].effort], ['opus', 'high'])
  assert.deepEqual([p.state.opts['task 2'].model, p.state.opts['review 2'].model, p.state.opts['review 2'].effort], ['haiku', 'sonnet', 'medium'])
  const t1 = result.tasks.find(t => t.task === '1')
  const t2 = result.tasks.find(t => t.task === '2')
  assert.deepEqual([t1.riskTag, t1.startModel, t1.reviewModel], ['safety', 'opus', 'opus'])
  assert.deepEqual([t2.riskTag, t2.startModel, t2.reviewModel], ['normal', 'haiku', 'sonnet'])
  // escapes metric: the review summary records which reviewer judged the task
  assert.deepEqual([t1.review.model, t1.review.effort], ['opus', 'high'])
  assert.deepEqual([t2.review.model, t2.review.effort], ['sonnet', 'medium'])
  assert.ok(logs.some(l => /Task 1: risk safety, start opus\/medium \(routed\), review opus\/high/.test(l)), logs.join('\n'))
})

await test('risk: implementer.risky_model sets the start; escalation climbs only above it', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 2 } })
  const agents = TIERED({ implementer: { risky_model: 'sonnet' } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxRetries: 1, agents, risk: { 1: 'safety' } } })
  assert.deepEqual(modelsOf(p.state, 'task 1'), ['sonnet', 'sonnet', 'opus'])
  assert.deepEqual(result.tasks[0].escalations, [{ stage: 'retry', model: 'opus' }])
  assert.equal(result.tasks[0].startModel, 'sonnet')
})

await test('risk: a safety task revision runs at its start tier, and re-reviews stay risky', async () => {
  const p = project({ deps: { 1: [] }, reviews: { 1: ['changes', 'approve'] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: TIERED(), risk: { 1: 'safety' } } })
  assert.equal(p.state.opts['task 1 revise 1'].model, 'opus')
  assert.equal(p.state.opts['review 1 round 2'].model, 'opus')
})

await test('risk: the risky reviewer never steps below the normal reviewer', async () => {
  const p = project({ deps: { 1: [] } })
  const agents = TIERED({ reviewer: { model: 'fable', effort: 'xhigh', risky_model: 'opus', risky_effort: 'high' } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents, risk: { 1: 'safety' } } })
  assert.deepEqual([p.state.opts['review 1'].model, p.state.opts['review 1'].effort], ['fable', 'xhigh'])
})

await test('reviewer tiers: a normal task gets the cheaper reviewer.model', async () => {
  const p = project({ deps: { 1: [] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: TIERED() } })
  assert.deepEqual([p.state.opts['review 1'].model, p.state.opts['review 1'].effort], ['sonnet', 'medium'])
  assert.equal(result.tasks[0].riskTag, 'normal')
  assert.equal(result.agents.reviewer, 'sonnet/medium')
  assert.equal(result.agents.reviewer_risky, 'opus/high')
})

await test('complexity routing: High starts at the first escalate tier, Low/Medium at the role model', async () => {
  const p = project({ deps: { 1: [], 2: [], 3: [] } })
  const agents = TIERED({ implementer: { complexity_routing: true } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents, complexity: { 1: 'high', 2: 'Low', 3: 'medium' } } })
  assert.deepEqual(['task 1', 'task 2', 'task 3'].map(l => p.state.opts[l].model), ['sonnet', 'haiku', 'haiku'])
  assert.equal(result.tasks.find(t => t.task === '1').startModel, 'sonnet')
})

await test('complexity routing: off by default, so High still starts on the cheap model', async () => {
  const p = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: TIERED(), complexity: { 1: 'High' } } })
  assert.equal(p.state.opts['task 1'].model, 'haiku')
})

await test('complexity routing: escalation climbs from the start tier, skipping the cheap model', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 2 } })
  const agents = TIERED({ implementer: { complexity_routing: true } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxRetries: 1, agents, complexity: { 1: 'high' } } })
  assert.deepEqual(modelsOf(p.state, 'task 1'), ['sonnet', 'sonnet', 'opus'])
  assert.deepEqual(result.merged, ['1'])
})

await test('risk: a safety test-track task also starts high and gets the risky reviewer', async () => {
  const p = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: TIERED(), tracks: { 1: 'test' }, risk: { 1: 'safety' } } })
  assert.equal(p.state.opts['task 1 [test]'].model, 'opus')
  assert.equal(p.state.opts['review 1'].model, 'opus')
})

await test('risk: a one-run --model override wins over routing for code-writing agents', async () => {
  const p = project({ deps: { 1: [] } })
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: TIERED(), model: 'haiku', risk: { 1: 'safety' } } })
  assert.equal(p.state.opts['task 1'].model, 'haiku')
  assert.equal(p.state.opts['review 1'].model, 'opus')  // the reviewer still follows risk
})

await test('risk: a bad risky model is refused before any agent runs', async () => {
  const p = project({ deps: { 1: [] } })
  await assert.rejects(runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: TIERED({ reviewer: { risky_model: 'gpt-5' } }) } }), /unknown model/)
  assert.equal(p.state.events.length, 0)
})

// --- batch landing -------------------------------------------------------------

// Task 1 finishes first and its landing is slow; 2, 3 and 4 finish meanwhile and queue up.
const QUEUED = { deps: { 1: [], 2: [], 3: [], 4: [] }, durations: { 1: 2, 2: 10, 3: 10, 4: 10 }, mergeDurations: { 1: 60 } }

await test('landing: tasks waiting together land in one ck worktree land step', async () => {
  const p = project(QUEUED)
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.deepEqual(p.state.landings, [['1'], ['2', '3', '4']])
  assert.equal(p.state.maxMerging, 1, 'one landing at a time')
  assert.match(p.state.prompts['land 2 3 4'], /ck -C \/repo worktree land demo 2 3 4 --into integrate\/demo --gate --install --release --json/)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'])
  assert.deepEqual(result.landings.map(l => l.size), [1, 3])
  assert.equal(result.stats.landings, 2)
  assert.ok(p.state.events.includes('final gate'))
})

await test('landing: a red batch is bisected by ck and only the culprit goes to the fixer', async () => {
  const p = project({ ...QUEUED, checkRed: ['3'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  const fixes = p.state.events.filter(e => e.startsWith('fix'))
  assert.deepEqual(fixes, ['fix 1 (Task 3 red on landing)'])
  assert.deepEqual(result.landings[1].red, ['3'])
  assert.deepEqual(result.landings[1].merged, ['2', '4'])
  assert.equal(result.landings[1].bisected, true)
  assert.deepEqual(p.state.landings[2], ['3'], 'the fixed culprit lands again')
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'])
  assert.equal(result.targetGreen, true)
  assert.equal(result.stats.bisected, 1)
})

await test('landing: the batch window halves after a red batch and grows after a green one', async () => {
  const deps = Object.fromEntries(['1', '2', '3', '4', '5', '6', '7'].map(n => [n, []]))
  const durations = { 1: 2, 2: 10, 3: 10, 4: 10, 5: 10, 6: 10, 7: 10 }
  const p = project({ deps, durations, mergeDurations: { 1: 60 }, checkRed: ['3'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, mergeBatch: 4 } })
  assert.deepEqual(result.landings.map(l => l.window), [4, 4, 2, 3])
  assert.deepEqual(result.landings.map(l => l.size), [1, 4, 2, 1])
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4', '5', '6', '7'])
})

await test('landing: mergeBatch 1 lands one task per step', async () => {
  const p = project(QUEUED)
  await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, mergeBatch: 1 } })
  assert.deepEqual(p.state.landings, [['1'], ['2'], ['3'], ['4']])
})

await test('landing: a task deferred for conflicting with a batch-mate lands first in the next batch', async () => {
  const p = project({ ...QUEUED, pairConflicts: [['2', '3']] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.deepEqual(result.landings[1].deferred, ['3'])
  assert.deepEqual(p.state.landings[2], ['3'])
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'])
  assert.ok(!p.state.events.some(e => e.startsWith('resolve')), 'a deferral is not a conflict')
})

await test('landing: a conflict in a batch goes to the resolver while the rest of the batch lands', async () => {
  const p = project({ ...QUEUED, conflicts: ['3'], agents: ROLES })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, agents: ROLES } })
  assert.deepEqual(result.landings[1].conflict, ['3'])
  assert.deepEqual(result.landings[1].merged, ['2', '4'])
  assert.ok(p.state.events.includes('resolve 3'))
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'])
})
