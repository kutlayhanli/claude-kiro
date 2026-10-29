// Prints every agent label the two real workflow scripts emit, as JSON on stdout:
// [{workflow, label, phase}]. Run: node tests/js/emit_labels.mjs
// The scripts run for real through load_workflow.mjs; only `agent` is stubbed, by the small
// scenario-driven fake below. The scenarios cover every label template family of the
// roles grammar (design.md S5): retry, escalation, review rounds, conflict rounds, landing
// fix, full and final gate, re-check, cross-test, plan retry, report, every spec-create phase.
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, labelCorpus } from './load_workflow.mjs'

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

// A scenario fake: knobs drive which labels the script reaches.
function fake({ deps, failUntil = {}, reviews = {}, conflictRounds = {}, redLands = {}, recheckRed = 0, fullRed = false, planFailFirst = false }) {
  const done = new Set()
  const attempts = {}
  const reviewCount = {}
  const hit = {}
  const red = { ...redLands }
  let recheckLeft = recheckRed
  let planCalls = 0
  return async (prompt, opts) => {
    const l = opts.label
    if (l.startsWith('plan')) {
      planCalls++
      if (planFailFirst && planCalls === 1) return null
      const ready = Object.keys(deps).filter(n => !done.has(n) && deps[n].every(d => done.has(d)))
      const exclude = ((prompt.match(/--exclude ([\d,]+)/) || [])[1] || '').split(',').filter(Boolean)
      return { done: [...done], deps, ready: ready.filter(n => !exclude.includes(n)), busy: [], remaining: Object.keys(deps).filter(n => !done.has(n)), cycles: [], verifyCycles: [] }
    }
    if (l.startsWith('task')) {
      const n = l.match(/task (\d+)/)[1]
      attempts[n] = (attempts[n] || 0) + 1
      const pass = attempts[n] > (failUntil[n] || 0)
      return { task: n, status: pass ? 'done' : 'in_progress', gatePassed: pass, summary: `attempt ${attempts[n]}`, commits: 'abc123', startedAt: '2026-10-04T10:00:00Z', finishedAt: '2026-10-04T10:01:00Z' }
    }
    if (l.startsWith('land ')) {
      const out = []
      for (const n of l.slice(5).split(' ')) {
        if ((hit[n] || 0) < (conflictRounds[n] || 0)) { hit[n] = (hit[n] || 0) + 1; out.push({ task: n, state: 'CONFLICT', files: ['specs/demo/tasks.md'] }); continue }
        if ((red[n] || 0) > 0) { red[n]--; out.push({ task: n, state: 'RED', checkGreen: false, checkTail: `red: task ${n}` }); continue }
        done.add(n)
        out.push({ task: n, state: 'MERGED', checkGreen: true, mergedAt: '2026-10-04T10:02:00Z' })
      }
      return { tasks: out, gates: [{ tasks: [], green: true }], bisected: false, startedAt: '2026-10-04T10:01:00Z', finishedAt: '2026-10-04T10:02:00Z' }
    }
    if (l.startsWith('resolve')) return { resolved: true, conflicts: 'kept both' }
    if (l.startsWith('review')) {
      const n = l.match(/review (\d+)/)[1]
      reviewCount[n] = (reviewCount[n] || 0) + 1
      const verdict = (reviews[n] || [])[reviewCount[n] - 1] || 'approve'
      return { verdict, findings: verdict === 'changes' ? [{ severity: 'blocking', issue: 'contradicts design.md' }] : [], summary: verdict }
    }
    if (l.startsWith('cross-test')) return { verdict: 'approve', findings: [], summary: 'consistent' }
    if (l === 'report') return { head: 'f00d', merged: [...done].map(n => ({ task: n, commit: `m${n}`, mergedAt: '2026-10-04T11:00:00Z' })), done: [], failed: [], blocked: [], unstarted: [] }
    if (l.startsWith('fix')) return { green: true }
    if (l.startsWith('re-check')) return { green: recheckLeft-- <= 0 }
    if (l.startsWith('full gate')) return { green: !fullRed }
    if (l === 'final gate') return { green: true }
    throw new Error('unexpected agent ' + l)
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
]

for (const { tracks, fullGateEvery, mergeBatch, ...knobs } of scenarios) {
  const args = {
    ...baseArgs,
    agents: AGENTS,
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
async function createAgent(prompt, opts) {
  const l = opts.label
  if (l.startsWith('write') || l.startsWith('link')) return { path: 'x', summary: 's', assumptions: [], concerns: [] }
  if (l === 'review: fidelity') return { findings: [{ id: 'R1', file: 'requirements.md', location: '1', severity: 'major', problem: 'p', evidence: 'e', fix: 'f', needsUserDecision: false }] }
  if (l === 'refute: fidelity') return { verdicts: [{ id: 'R1', refuted: false, reason: 'real' }] }
  if (l.startsWith('review:')) return { findings: [] }
  if (l.startsWith('revise')) return { fixed: ['R1'], notFixed: [], escalated: [], result: 'Ready', schedule: 'Ready at start: Task 1' }
  if (l === 'snapshot spec before revision') return { copied: ['requirements.md'] }
  if (l.startsWith('consistency check')) return { contradictions: [] }
  if (l === 'remove pre-revision snapshot') return { removed: true }
  throw new Error('unexpected ' + l)
}
await runWorkflow(CREATE, { agent: createAgent, args: createArgs })

process.stdout.write(JSON.stringify(labelCorpus, null, 1) + '\n')
