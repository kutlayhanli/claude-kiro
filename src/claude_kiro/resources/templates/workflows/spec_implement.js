export const meta = {
  name: 'spec-implement',
  description: 'Implement a spec: start each task as soon as its dependencies merge, gate-checked retry, merges one at a time with a per-merge check, full gate every N merges and at the end',
  whenToUse: 'Invoked by /spec:implement <spec> with no task number',
}

// args (built by /spec:implement from `ck plan <spec> --json`):
// {
//   spec: 'market-sim',                    // spec directory name under specs/
//   root: '/abs/main/checkout',            // where `ck worktree` puts .claude/worktrees/
//   into: 'integrate/market-sim',          // branch task work is merged into (default flow: the integration branch)
//   targetDir: '/abs/checkout/of/into',    // checkout that has `into` checked out (integration worktree, or root)
//   wave: { '1': 1, ... },                 // display grouping only (from ck plan)
//   titles: { '1': '...' }, tracks: { '1': 'test' },
//   maxRetries: 1,                         // extra attempts per task after a failed gate or a no-op attempt
//   onFailure: 'continue' | 'halt',        // default continue: other ready tasks keep starting; a red target always halts
//   maxConcurrent: null,                   // cap on task agents at once (memory-heavy test suites)
//   fullGateEvery: 10,                     // run the full `ck gate <spec>` every N merges (0 = only at the end)
//   model: null,                           // task, retry and fix agents: 'sonnet' | 'opus' | 'haiku' | 'fable' (latest of that
//                                          //   family) or a full model ID; null = inherit the session model
//   effort: null,                          // same agents: 'low' | 'medium' | 'high' | 'xhigh' | 'max'; null = inherit
// }
//
// Scheduling is driven by the plan in tasks.md on the target branch, re-read
// (`ck plan`) every time a task finishes, so relinks and edited dependencies
// apply without restarting. A task starts as soon as all its Dependencies are
// Done on the target; waves are only used to group the progress display.
// Resuming: run /spec:implement again (a fresh run skips Done tasks); don't use
// resumeFromRunId, whose cached plan steps would be stale.
const A = args || {}
const SPEC = A.spec
const ROOT = A.root
const INTO = A.into || 'main'
const TARGET = A.targetDir || ROOT
const WAVE = A.wave || {}
const TITLES = A.titles || {}
const TRACKS = A.tracks || {}
const MAX_RETRIES = Number.isInteger(A.maxRetries) ? A.maxRetries : 1
const CONTINUE = A.onFailure !== 'halt'
const MAX_CONCURRENT = A.maxConcurrent && A.maxConcurrent > 0 ? A.maxConcurrent : Infinity
const FULL_EVERY = Number.isInteger(A.fullGateEvery) ? A.fullGateEvery : 10
const BRIEF = `${ROOT}/.claude/workflows/spec-implement-brief.md`

if (!SPEC || !ROOT) throw new Error('spec-implement needs args.spec and args.root')

// Model and effort for the agents that write code (task, retry, fix). Unset means
// inherit the session's; planning and merge steps keep their own low effort.
const MODEL = A.model || null
const EFFORT = A.effort || null
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
if (MODEL && !/^(sonnet|opus|haiku|fable)$|^claude-/.test(MODEL)) throw new Error(`spec-implement: unknown model "${MODEL}" (use sonnet, opus, haiku, fable, or a claude-* ID)`)
if (EFFORT && !EFFORTS.includes(EFFORT)) throw new Error(`spec-implement: unknown effort "${EFFORT}" (use ${EFFORTS.join(', ')})`)
const IMPL = { ...(MODEL ? { model: MODEL } : {}), ...(EFFORT ? { effort: EFFORT } : {}) }
if (MODEL || EFFORT) log(`Task agents: model ${MODEL || 'inherited'}, effort ${EFFORT || 'inherited'}`)

const WT = n => `${ROOT}/.claude/worktrees/${SPEC}-task-${n}`
const BR = n => `feat/${SPEC}-task-${n}`
const waveOf = n => WAVE[n] || 0
const phaseOf = n => (waveOf(n) ? `Wave ${waveOf(n)}` : 'Tasks')
const label = n => `task ${n}${TRACKS[n] === 'test' ? ' [test]' : ''}`
const NOW = 'date -u +%Y-%m-%dT%H:%M:%SZ'

const PLAN = {
  type: 'object',
  properties: {
    done: { type: 'array', items: { type: 'string' }, description: '"done" from ck plan' },
    ready: { type: 'array', items: { type: 'string' }, description: 'tasks whose worktree is CREATED or EXISTS (from ck worktree create)' },
    busy: {
      type: 'array',
      items: { type: 'object', properties: { task: { type: 'string' }, detail: { type: 'string' } }, required: ['task', 'detail'] },
    },
    remaining: { type: 'array', items: { type: 'string' }, description: '"remaining" from ck plan' },
    cycles: { type: 'array', items: { type: 'array', items: { type: 'string' } }, description: '"cycles" from ck plan' },
    verifyCycles: {
      type: 'array',
      items: { type: 'object', properties: { task: { type: 'string' }, message: { type: 'string' } }, required: ['task', 'message'] },
      description: '"verifyCycles" from ck plan: task and message only',
    },
    error: { type: 'string' },
  },
  required: ['done', 'ready', 'busy', 'remaining', 'cycles', 'verifyCycles'],
}
const RESULT = {
  type: 'object',
  properties: {
    task: { type: 'string' },
    status: { type: 'string', enum: ['done', 'in_progress', 'blocked'] },
    gatePassed: { type: 'boolean' },
    summary: { type: 'string' },
    commits: { type: 'string', description: 'hashes of commits you made; empty if none' },
    gateTail: { type: 'string' },
    blocker: { type: 'string' },
    deviations: { type: 'string' },
    wrongTests: { type: 'string' },
    outsideFiles: { type: 'string' },
    startedAt: { type: 'string', description: `output of "${NOW}" when you started` },
    finishedAt: { type: 'string', description: `output of "${NOW}" when you finished` },
  },
  required: ['task', 'status', 'gatePassed', 'summary', 'commits'],
}
const MERGE = {
  type: 'object',
  properties: {
    merged: { type: 'boolean' },
    conflicts: { type: 'string', description: 'files that conflicted and how they were resolved, or empty' },
    error: { type: 'string', description: 'why it did not merge, including a refused/blocked command' },
    checkGreen: { type: 'boolean', description: 'ck gate --task on the target after the merge exited 0' },
    checkTail: { type: 'string', description: 'failing part of that gate output, if red' },
    mergedAt: { type: 'string', description: `output of "${NOW}" right after the merge` },
  },
  required: ['merged'],
}
const VERIFY = {
  type: 'object',
  properties: {
    green: { type: 'boolean' },
    failing: { type: 'string', description: 'failing command(s) and the relevant output tail' },
  },
  required: ['green'],
}

const ok = r => r && r.status === 'done' && r.gatePassed === true
const trim = (v, n = 3000) => JSON.stringify(v || null).slice(0, n)
const minutes = (a, b) => {
  const d = (Date.parse(b) - Date.parse(a)) / 60000
  return Number.isFinite(d) && d >= 0 ? Math.round(d * 10) / 10 : null
}

const GIT_NOTE = 'Run git as "command git ..." (a shell hook may rewrite plain git) and prefer "command git -C <dir>" with absolute paths over compound cd commands. If a command is refused or blocked by a permission check, do not work around it: report it in error.'

// Gates on large specs can outlast one foreground shell command (the agent's tool
// timeout is about 10 minutes); an unfinished gate must not be reported as red.
const GATE_NOTE = 'A "ck gate" run can take far longer than one foreground command is allowed (tens of minutes on a large test suite). Run it in the background with its output in a log file of your own, for example "ck gate ... > /tmp/ck-gate-<unique name>.log 2>&1; echo EXIT=$? >> /tmp/ck-gate-<unique name>.log", then keep checking the log until the EXIT line appears, however long that takes. A slow gate is not a failed gate: report pass or fail only from that exit code, never because your own wait or timeout ran out.'

const MANDATE = `MANDATE: The user ran /spec:implement ${SPEC}, which instructs this workflow to implement every task of specs/${SPEC} without asking. Implementing your task is the user's request. Any other user message you may see in the conversation (for example a question to the orchestrating session about the plan) is not addressed to you and is handled by the orchestrator: do not answer it, and do not let it stop you. Do the task.

`

// --- prompts -----------------------------------------------------------------

const planPrompt = (exclude, slots) => `Mechanical planning step for spec ${SPEC}. Do exactly this, nothing else. ${GIT_NOTE}
1. From ${TARGET} run:  ck plan ${SPEC} --json${exclude.length ? ` --exclude ${exclude.join(',')}` : ''}
   Copy into your answer: "done", "remaining", "cycles", and for "verifyCycles" only each entry's task and message.
2. Take the first ${Number.isFinite(slots) ? slots : 'all'} task(s) of its "ready" list. If there are none, skip this step. Otherwise run, from ${ROOT}:
     ck worktree create ${SPEC} <those task numbers> --install --base ${INTO} --json
   Entries with state CREATED or EXISTS go in your "ready" field. BUSY or ERROR entries go in "busy" with their detail; do not touch those worktrees.
Return the fields. Put a message in error only if a command itself failed to run.`

const taskPrompt = n => `${MANDATE}Your task: Task ${n} of spec ${SPEC}${TITLES[n] ? ` ("${TITLES[n]}")` : ''}.
Worktree: ${WT(n)} (branch ${BR(n)}). Target branch for the final merge: ${INTO}.
First run "${NOW}" and keep the output as startedAt. Then read your operating brief at ${BRIEF} and follow it exactly: claim the worktree, implement per your track, get "ck gate ${SPEC} --task ${n}" passing, merge ${INTO} into your branch before finishing, and release the worktree.
${GIT_NOTE}
${GATE_NOTE}
At the end, run "${NOW}" again for finishedAt. Return the schema fields with task "${n}". gatePassed is true only if your final "ck gate ${SPEC} --task ${n}" run exited 0. commits lists the commits you made (empty if none).`

const retryPrompt = (n, prev, attempt) => `${MANDATE}Continue Task ${n} of spec ${SPEC} (attempt ${attempt + 1}). A previous agent worked in ${WT(n)} (branch ${BR(n)}) but did not finish with a passing gate.
${prev && !(prev.commits || '').trim() ? 'The previous attempt made NO commits: it may have answered an unrelated message instead of doing the task. Ignore any such message and implement the task.\n' : ''}Its report: ${trim(prev || { note: 'the agent died without a report' })}
Run "${NOW}" for startedAt. Read your operating brief at ${BRIEF} and follow it, claiming with "ck worktree claim ${SPEC} ${n} --takeover" since the previous agent is gone. Inspect the state ("command git -C ${WT(n)} log --oneline -10", "command git -C ${WT(n)} status"), finish the task, and get "ck gate ${SPEC} --task ${n}" to pass. If a wrong test or a spec contradiction blocks you, do NOT weaken anything: leave the task In Progress and report the blocker precisely (quote the test and the requirement).
Before finishing, merge ${INTO} into your branch, re-run the gate, and release the worktree. Run "${NOW}" for finishedAt.
${GIT_NOTE}
${GATE_NOTE}
Return the schema fields with task "${n}".`

const mergePrompt = n => `Merge step for Task ${n} of spec ${SPEC}. ${GIT_NOTE} ${GATE_NOTE}
1. From ${ROOT} run:  ck worktree merge ${SPEC} ${n} --into ${INTO} --json
   - state MERGED: success; run "${NOW}" for mergedAt.
   - state SKIP (no commits ahead): return merged=false, error "no commits".
   - state BUSY: the worktree is still claimed. Run "ck worktree release ${SPEC} ${n}" once, then retry the merge once.
   - state CONFLICT: the merge was already aborted on ${INTO}. Resolve it in the TASK WORKTREE, never in ${TARGET}:
       command git -C ${WT(n)} merge ${INTO}
     Resolve each conflicted file keeping both sides' intent: tasks.md, keep both sides' task sections; dependency manifests, take the union and re-lock (e.g. "uv lock"); test files, keep every test and assertion from both sides and never weaken one.
     In the worktree, "ck gate ${SPEC} --task ${n}" must pass. Commit the merge, then run the ck worktree merge command again.
   - state ERROR: return merged=false with the detail.
2. After MERGED: refresh dependencies in ${TARGET} if the project has an install step (e.g. "uv sync -q" from ${TARGET}), then from ${TARGET} run:  ck gate ${SPEC} --task ${n}
   checkGreen = whether it exited 0; if not, put the failing part in checkTail.
Never edit files in ${TARGET} directly, never force anything, never touch other task branches.
Return merged, conflicts, error, checkGreen, checkTail, mergedAt.`

const verifyPrompt = what => `Verification (${what}) for spec ${SPEC}. In ${TARGET} (branch ${INTO}):
1. Refresh dependencies if the project has an install step (e.g. "uv sync -q").
2. Run:  ck gate ${SPEC}
   It checks every Done task: acceptance boxes, the tests that should pass by now, test collection, and that no test was weakened.
${GATE_NOTE}
Do not modify anything. Return green=true only if "ck gate ${SPEC}" exited 0; otherwise put the failing checks and the relevant output tail in failing.`

const recheckPrompt = n => `Re-check for spec ${SPEC}. In ${TARGET} (branch ${INTO}): refresh dependencies if needed (e.g. "uv sync -q"), then run "ck gate ${SPEC}${n ? ` --task ${n}` : ''}". ${GATE_NOTE} Do not modify anything. Return green=true only if it exited 0; otherwise put the failing part in failing.`

const fixPrompt = (k, what, failure) => `${INTO} is red (${what}) for spec ${SPEC}. Failure: ${trim(failure, 4000)}
Fix it without weakening any test and without editing requirements.md. ${GIT_NOTE} ${GATE_NOTE}
1. From ${ROOT}: ck worktree create ${SPEC} fix${k} --install --base ${INTO}   (worktree ${WT(`fix${k}`)}). Work only there.
2. Diagnose with the failing check, fix the code (or a clearly broken fixture or harness), and confirm the failing check passes in the fix worktree.
3. Commit with a message starting "fix:", then from ${ROOT}: ck worktree merge ${SPEC} fix${k} --into ${INTO}
If the only way to green is to weaken a test or change a requirement, stop and explain precisely instead.
Return green=true only if ${INTO} is green after your merge, else failing with the reason.`

// --- scheduler ---------------------------------------------------------------

const running = new Map()    // task -> promise of its record
const records = {}           // task -> record
const failed = new Map()     // task -> reason
const busyReported = new Set()
let stopLaunching = false
let stopReason = null
let redTarget = false
let mergedCount = 0
let fixCount = 0
let refills = 0
let mergeChain = Promise.resolve()
const fullGates = []

async function plan() {
  refills++
  const exclude = [...running.keys(), ...failed.keys()]
  const slots = MAX_CONCURRENT - running.size
  const p = await agent(planPrompt(exclude, slots), { label: `plan ${refills}`, phase: 'Plan', schema: PLAN, effort: 'low' })
  if (!p || p.error) return null
  return p
}

async function fixAndRecheck(what, failure, n) {
  fixCount++
  const fix = await agent(fixPrompt(fixCount, what, failure), { label: `fix ${fixCount} (${what})`, phase: n ? phaseOf(n) : 'Final gate', schema: VERIFY, ...IMPL })
  const recheck = await agent(recheckPrompt(n), { label: `re-check ${fixCount}`, phase: n ? phaseOf(n) : 'Final gate', schema: VERIFY })
  return { fix, recheck, green: !!(recheck && recheck.green) }
}

function enqueueMerge(n) {
  const link = mergeChain.then(async () => {
    const m = await agent(mergePrompt(n), { label: `merge ${n}`, phase: phaseOf(n), schema: MERGE, effort: 'low' })
    if (!m || !m.merged) return m
    mergedCount++
    let green = m.checkGreen === true
    if (!green) {
      log(`Task ${n}: ${INTO} red after merge, one fix attempt`)
      const f = await fixAndRecheck(`after merging Task ${n}`, m.checkTail, n)
      m.fix = f
      green = f.green
    }
    if (green && FULL_EVERY > 0 && mergedCount % FULL_EVERY === 0) {
      let v = await agent(verifyPrompt(`full gate after ${mergedCount} merges`), { label: `full gate @${mergedCount}`, phase: phaseOf(n), schema: VERIFY })
      if (!v || !v.green) {
        log(`Full gate red after ${mergedCount} merges, one fix attempt`)
        const f = await fixAndRecheck(`full gate after ${mergedCount} merges`, v)
        v = { ...(v || {}), fix: f, green: f.green }
      }
      fullGates.push({ afterMerges: mergedCount, green: !!v.green })
      green = !!v.green
    }
    if (!green) {
      redTarget = true
      stopLaunching = true
      stopReason = stopReason || `${INTO} is red after merging Task ${n}`
    }
    return m
  })
  mergeChain = link.catch(() => null)
  return link.catch(() => null)
}

async function runTask(n) {
  const rec = { task: n, wave: waveOf(n), title: TITLES[n], attempts: 0, ok: false }
  try {
    let r = await agent(taskPrompt(n), { label: label(n), phase: phaseOf(n), schema: RESULT, ...IMPL })
    rec.attempts = 1
    rec.startedAt = r && r.startedAt
    while (!ok(r) && rec.attempts <= MAX_RETRIES && !(r && r.blocker && /worktree busy/i.test(r.blocker))) {
      log(`Task ${n}: ${r && !(r.commits || '').trim() ? 'no commits' : 'gate not passed'}, retry ${rec.attempts}/${MAX_RETRIES}`)
      r = await agent(retryPrompt(n, r, rec.attempts), { label: `${label(n)} retry ${rec.attempts}`, phase: phaseOf(n), schema: RESULT, ...IMPL })
      rec.attempts++
      rec.startedAt = rec.startedAt || (r && r.startedAt)
    }
    rec.result = r
    rec.finishedAt = r && r.finishedAt
    if (!ok(r)) {
      rec.reason = r ? r.blocker || `gate not passed (status ${r.status})` : 'agent died without a report'
      return rec
    }
    const m = await enqueueMerge(n)
    rec.merge = m
    rec.mergedAt = m && m.mergedAt
    if (!m || !m.merged) {
      rec.reason = `merge failed: ${(m && m.error) || 'no result'}`
      return rec
    }
    rec.ok = true
    log(`Task ${n}: done and merged into ${INTO}`)
    return rec
  } catch (e) {
    rec.reason = `error: ${e && e.message}`
    return rec
  }
}

function launch(p) {
  for (const b of p.busy || []) {
    const n = String(b.task)
    if (!busyReported.has(n) && !running.has(n)) {
      busyReported.add(n)
      failed.set(n, `worktree busy: ${b.detail}`)
      records[n] = { task: n, wave: waveOf(n), ok: false, reason: `worktree busy: ${b.detail}` }
    }
  }
  for (const n of p.ready.map(String)) {
    if (stopLaunching || running.size >= MAX_CONCURRENT) break
    if (running.has(n) || failed.has(n) || (records[n] && records[n].ok) || p.done.includes(n)) continue
    running.set(n, runTask(n).then(rec => ({ n, rec })))
  }
}

function applyLint(p) {
  for (const vc of p.verifyCycles || []) {
    const n = String(vc.task)
    if (!failed.has(n) && !running.has(n)) {
      failed.set(n, `verify-order cycle: ${vc.message}`)
      records[n] = { task: n, wave: waveOf(n), ok: false, reason: `verify-order cycle: ${vc.message}` }
      log(`Task ${n}: skipped, verify-order cycle (fix tasks.md; the next plan step picks it up)`)
      if (!CONTINUE) { stopLaunching = true; stopReason = stopReason || `verify-order cycle at Task ${n}` }
    }
  }
  if ((p.cycles || []).length) {
    stopLaunching = true
    stopReason = stopReason || `dependency cycle: ${p.cycles.map(c => c.join(' -> ')).join('; ')}`
  }
}

phase('Plan')
let current = await plan()
if (!current) return { halted: true, refused: false, reason: 'planning step failed', into: INTO }
if (current.cycles.length || current.verifyCycles.length) {
  log('Refusing to launch: the plan has a cycle. Fix tasks.md (see `ck lint`) and run again.')
  return {
    halted: true,
    refused: true,
    reason: 'plan has a cycle; nothing was started',
    cycles: current.cycles,
    verifyCycles: current.verifyCycles,
    into: INTO,
  }
}
const initiallyDone = current.done.length
log(`${current.remaining.length} task(s) remaining, ${initiallyDone} already Done; starting ${Math.min(current.ready.length, MAX_CONCURRENT)}`)
launch(current)

while (running.size) {
  const { n, rec } = await Promise.race(running.values())
  running.delete(n)
  records[n] = rec
  if (!rec.ok) {
    failed.set(n, rec.reason)
    log(`Task ${n}: not merged (${rec.reason})`)
    if (!CONTINUE) { stopLaunching = true; stopReason = stopReason || `Task ${n} failed` }
  }
  if (stopLaunching) continue  // let running tasks finish and merge
  const next = await plan()
  if (!next) { stopLaunching = true; stopReason = 'planning step failed'; continue }
  current = next
  applyLint(current)
  launch(current)
}
await mergeChain

// Final full gate on the target, unless it is already known to be red.
let finalGate = null
if (mergedCount && !redTarget) {
  phase('Final gate')
  finalGate = await agent(verifyPrompt('final'), { label: 'final gate', phase: 'Final gate', schema: VERIFY })
  if (!finalGate || !finalGate.green) {
    log(`Final gate red, one fix attempt`)
    const f = await fixAndRecheck('final gate', finalGate)
    finalGate = { ...(finalGate || {}), fix: f, green: f.green }
  }
  if (!finalGate.green) redTarget = true
}

// --- report ------------------------------------------------------------------

const taskReport = Object.values(records).map(r => ({
  task: r.task,
  wave: r.wave,
  title: r.title,
  ok: r.ok,
  attempts: r.attempts,
  startedAt: r.startedAt,
  finishedAt: r.finishedAt,
  mergedAt: r.mergedAt,
  agentMinutes: minutes(r.startedAt, r.finishedAt),
  toMergedMinutes: minutes(r.startedAt, r.mergedAt),
  reason: r.reason,
  summary: r.result && r.result.summary,
  wrongTests: r.result && r.result.wrongTests,
  outsideFiles: r.result && r.result.outsideFiles,
  deviations: r.result && r.result.deviations,
  fixedAfterMerge: !!(r.merge && r.merge.fix),
}))
const spans = {}
for (const t of taskReport) {
  const key = t.wave ? `Wave ${t.wave}` : 'Tasks'
  const end = t.mergedAt || t.finishedAt
  if (!t.startedAt) continue
  const s = spans[key] || (spans[key] = { group: key, tasks: 0, start: t.startedAt, end })
  s.tasks++
  if (Date.parse(t.startedAt) < Date.parse(s.start)) s.start = t.startedAt
  if (end && (!s.end || Date.parse(end) > Date.parse(s.end))) s.end = end
}
const phases = Object.values(spans).map(s => ({ ...s, minutes: minutes(s.start, s.end) }))
const starts = taskReport.map(t => t.startedAt).filter(Boolean).sort((a, b) => Date.parse(a) - Date.parse(b))
const ends = taskReport.map(t => t.mergedAt || t.finishedAt).filter(Boolean).sort((a, b) => Date.parse(a) - Date.parse(b))
const merged = taskReport.filter(t => t.ok).map(t => t.task)
const notStarted = (current.remaining || []).map(String).filter(n => !records[n] && !running.has(n))

return {
  halted: redTarget || (!!stopReason && notStarted.length > 0),
  reason: stopReason,
  into: INTO,
  taskAgents: { model: MODEL || 'inherited', effort: EFFORT || 'inherited' },
  targetGreen: !redTarget,
  merged,
  failed: [...failed.entries()].map(([task, reason]) => ({ task, reason })),
  notStarted,
  timing: {
    start: starts[0] || null,
    end: ends[ends.length - 1] || null,
    minutes: minutes(starts[0], ends[ends.length - 1]),
    phases,
  },
  tasks: taskReport,
  fullGates,
  finalGate,
  stats: { initiallyDone, merged: merged.length, failed: failed.size, notStarted: notStarted.length, planSteps: refills, fixes: fixCount },
}
