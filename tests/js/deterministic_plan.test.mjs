// Deterministic-plan example cases (Stories 1-4: TC-1/TC-2/TC-4..TC-7, TC-9..TC-22, TC-26).
// Run: node tests/js/deterministic_plan.test.mjs [TC-id ...]  (no ids: run every registered case)
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { project } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')

export const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }

const cases = []
// TC bodies live with the tasks that own them (Tasks 5, 6, 12, 15, 19, 20); the runner and the
// TC-id argv filter below are this task's harness.
export function tc(id, name, fn) { cases.push({ id, name, fn }) }

const idx = (s, l) => s.events.indexOf(l)

tc('TC-0', 'harness smoke: a healthy 2-task run completes', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the real script requested a plan agent')
})

// Shared per-case helpers for the TC tasks (assert only on events/prompts/result/logs, never
// script internals).
export const helpers = { idx }

const only = new Set(process.argv.slice(2).filter(a => /^TC-\d+$/.test(a)))
const unknown = [...only].filter(id => !cases.some(c => c.id === id))

let failed = 0
for (const c of cases) {
  if (only.size && !only.has(c.id)) continue
  try { await c.fn(); console.log('ok  ', c.id, c.name) } catch (e) { failed++; console.log('FAIL', c.id, c.name); console.log(e) }
}
if (!cases.length) console.log('ok   (no TC cases registered yet)')
if (unknown.length) { failed++; console.log('FAIL unknown TC id(s):', unknown.join(' ')) }
if (!failed) console.log('deterministic_plan: all requested cases passed')
process.exitCode = failed ? 1 : 0
