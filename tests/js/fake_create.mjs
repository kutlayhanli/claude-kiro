// The stub agent for plugin/workflows/spec-create.js, shared by spec_create.test.mjs and the bridge's create op.
import { isCkCall, arbiterArgv, arbiterStdin } from './fake_project.mjs'

// The thin `ck` steps of spec-create (run through the runner agent, answering the CK_CALL shape), answered by argv rather than
// by label, since design.md pins no labels for them: `ck arbiter context|lane|review` and `ck progress event`. `cut` is
// 'hold' by default and 'frozen' once implementation has started. The fake plays `ck`: like `ck arbiter review`, it mints
// the ruling numbers itself (a counter in `minted`, starting at 1, one number per subject), never reading them from the
// subjects, which are findings or cuts and carry none. `ck` overrides answer a step in its place: the key is
// `ck arbiter review <step>` (the `--step` value), `ck arbiter context`, `ck arbiter lane` or `ck progress event`, and the
// value is the `json` to answer, or a function of (subjects, argv) returning it. Returns undefined for a call that is not
// a thin ck step.
export function thinCkDefault(prompt, opts, { implementationStarted = false, minted = { last: 0 }, ck = {} } = {}) {
  if (!isCkCall(opts)) return undefined
  const text = String(prompt)
  const argv = arbiterArgv(text) || []
  const rest = argv[0] === '-C' ? argv.slice(2) : argv
  const ok = json => ({ exitCode: 0, json })
  let subjects = []
  if (rest[0] === 'arbiter' && rest[1] === 'review') {
    try { subjects = JSON.parse(arbiterStdin(text) || '{}').subjects || [] } catch { /* none */ }
  }
  const stepOf = () => { const i = rest.indexOf('--step'); return i >= 0 ? rest[i + 1] : undefined }
  const key = rest[0] === 'arbiter'
    ? (rest[1] === 'review' ? `ck arbiter review ${stepOf()}` : `ck arbiter ${rest[1]}`)
    : (/progress event/.test(text) ? 'ck progress event' : undefined)
  if (key && key in ck) {
    const v = ck[key]
    return ok(typeof v === 'function' ? v(subjects, argv) : v)
  }
  if (rest[0] === 'arbiter') {
    if (rest[1] === 'context') return ok({ principles: [], planLayer: '', implementationStarted, missing: null, cut: implementationStarted ? 'frozen' : 'hold' })
    if (rest[1] === 'lane') return ok({ ok: true })
    if (rest[1] === 'review') {
      // A route action of CreateRoute (design.md), not a ruling outcome: the default ruling lets each subject stand as it was.
      const action = { refuted: 'keep-refuted', cuts: 'reject-cut', drift: 'report' }[stepOf()] || 'keep-refuted'
      return ok({ results: subjects.map((x, i) => ({ id: String((x && x.id) ?? i), action, reason: '', stronger: false, ruling: ++minted.last })), lost: false })
    }
  }
  if (/progress event/.test(text)) return ok({ ok: true })
  return undefined
}

// Returns {agent, calls, prompts}; `overrides` maps a label to the answer that replaces the default, and a `ck ...` key
// (see thinCkDefault) to the answer of that thin ck step. Ruling numbers are minted per stub, starting at 1.
export function stub(overrides = {}) {
  const ck = Object.fromEntries(Object.entries(overrides).filter(([k]) => k.startsWith('ck ')))
  const minted = { last: 0 }
  const calls = []
  const prompts = {}
  const agent = async (prompt, opts) => {
    calls.push(opts.label)
    prompts[opts.label] = prompt
    if (opts.label in overrides) return overrides[opts.label]
    const l = opts.label
    if (l.startsWith('write') || l.startsWith('link')) return { path: 'x', summary: 's', assumptions: [], concerns: [] }
    // run-principles: one call per arbiter step (a ` risky` suffix re-runs at the risky tier), answering the rulings of its
    // subjects (an empty list by default); `review: should-exist` finds no cut; `apply-cuts` applies none.
    // The thin ck steps around the arbiter: a usable principles file, implementation not started, cut 'hold', the lane opened or
    // closed, one result per subject, progress events recorded. Answered by argv, not by label.
    if (/^arbiter (refuted|cuts|drift)( risky)?$/.test(l)) return { rulings: [] }
    const thin = thinCkDefault(prompt, opts, { minted, ck })
    if (thin) return thin
    if (l === 'review: should-exist') return { findings: [] }
    if (l.startsWith('apply-cuts')) return { applied: [], summary: 'no cuts applied' }
    if (l === 'review: fidelity') return { findings: [
      { id: 'R1', file: 'requirements.md', location: '1', severity: 'major', problem: 'p', evidence: 'e', fix: 'f', needsUserDecision: false },
      { id: 'R2', file: 'requirements.md', location: '2', severity: 'minor', problem: 'p2', evidence: 'e', fix: 'f', needsUserDecision: true, question: 'q?' },
      { id: 'R3', file: 'requirements.md', location: '3', severity: 'minor', problem: 'p3', evidence: 'e', fix: 'f', needsUserDecision: false }] }
    if (l === 'refute: fidelity') return { verdicts: [{ id: 'R1', refuted: false, reason: 'real' }, { id: 'R2', refuted: false, reason: 'real' }, { id: 'R3', refuted: true, reason: 'style' }] }
    if (l.startsWith('review:')) return { findings: [] }
    if (l.startsWith('revise')) return { fixed: ['R1'], notFixed: [], escalated: [], result: 'Ready after decisions', schedule: 'Ready at start: Task 1. Critical path: Task 1' }
    if (l === 'snapshot spec before revision') return { copied: ['requirements.md', 'design.md', 'test-plan.md', 'tasks.md'] }
    if (l.startsWith('consistency check')) return { contradictions: [] }
    if (l === 'remove pre-revision snapshot') return { removed: true }
    throw new Error('unexpected ' + l)
  }
  return { agent, calls, prompts }
}
