// Load the pure decision block of plugin/workflows/spec-implement.js so tests can call the seams
// without the fake runtime. The block sits between two marker comments in the script.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
export const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')
export const START_MARKER = '// --- decisions (pure; loaded by tests) ---'
export const END_MARKER = '// --- end decisions ---'
export const NAMES = ['readyTasks', 'chainLengths', 'applyPlanView', 'planConsistent', 'planDecision', 'queuedAgents', 'startDecision', 'worktreeOutcome']

// Guard-ladder decisions: loaded only when asked, so the suite stays green before the workflow gains them.
export const GUARD_NAMES = ['guardLadder', 'guardStopOf', 'parkAction']

export function loadDecisions(scriptPath = SCRIPT, { extra = [] } = {}) {
  if (scriptPath && typeof scriptPath === 'object') { extra = scriptPath.extra ?? []; scriptPath = SCRIPT }
  const names = [...NAMES, ...extra]
  const src = fs.readFileSync(scriptPath, 'utf8')
  const start = src.indexOf(START_MARKER)
  if (start < 0) throw new Error(`decision marker missing in ${scriptPath}: "${START_MARKER}"`)
  const end = src.indexOf(END_MARKER, start)
  if (end < 0) throw new Error(`decision marker missing in ${scriptPath}: "${END_MARKER}"`)
  const block = src.slice(start + START_MARKER.length, end)
  let decisions
  try {
    decisions = new Function(`${block}\nreturn { ${names.map(n => `${n}: typeof ${n} === 'function' ? ${n} : undefined`).join(', ')} }`)()
  } catch (e) {
    throw new Error(`decision block of ${scriptPath} cannot be evaluated alone: ${e.message}`)
  }
  for (const name of names) if (typeof decisions[name] !== 'function') throw new Error(`decision function missing in the decisions block: ${name}`)
  return decisions
}
