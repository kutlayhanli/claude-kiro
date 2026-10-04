export const meta = {
  name: 'spec-implement',
  description: 'Implement a spec wave by wave: parallel task agents in worktrees with a gate-checked retry, merges one at a time as tasks finish, verify the target after each wave, halt on red',
  whenToUse: 'Invoked by /spec:implement <spec> with no task number',
}

// args (built by /spec:implement from `ck waves <spec> --json`):
// {
//   spec: 'market-sim',                    // spec directory name under specs/
//   root: '/abs/main/checkout',            // where `ck worktree` puts .claude/worktrees/
//   into: 'integrate/market-sim',          // branch task work is merged into (default flow: the integration branch)
//   targetDir: '/abs/checkout/of/into',    // checkout that has `into` checked out (integration worktree, or root)
//   waves: [['1'], ['2', '3'], ...],       // from `ck waves --json`
//   deps: { '3': ['1'], ... },             // from `ck waves --json`
//   titles: { '1': '...' }, tracks: { '1': 'test' },
//   maxRetries: 1,                         // extra attempts per task after a failed gate
//   onFailure: 'continue' | 'halt',        // default continue: keep running tasks whose dependencies all merged; a red target always halts
//   maxConcurrent: null,                   // cap on task agents at once (memory-heavy test suites)
//   startWave: 1,
// }
const A = args || {}
const SPEC = A.spec
const ROOT = A.root
const INTO = A.into || 'main'
const TARGET = A.targetDir || ROOT
const WAVES = (A.waves || []).map(w => w.map(String))
const DEPS = A.deps || {}
const TITLES = A.titles || {}
const TRACKS = A.tracks || {}
const MAX_RETRIES = Number.isInteger(A.maxRetries) ? A.maxRetries : 1
const CONTINUE = A.onFailure !== 'halt'
const START = Math.max(1, A.startWave || 1)
const BRIEF = `${ROOT}/.claude/workflows/spec-implement-brief.md`

if (!SPEC || !ROOT || !WAVES.length) throw new Error('spec-implement needs args.spec, args.root and args.waves')

const WT = n => `${ROOT}/.claude/worktrees/${SPEC}-task-${n}`
const BR = n => `feat/${SPEC}-task-${n}`
const label = n => `task ${n}${TRACKS[n] === 'test' ? ' [test]' : ''}`

const SETUP = {
  type: 'object',
  properties: {
    done: { type: 'array', items: { type: 'string' }, description: 'task numbers already Done on the target branch' },
    ready: { type: 'array', items: { type: 'string' }, description: 'task numbers whose worktree is CREATED or EXISTS' },
    busy: {
      type: 'array',
      items: { type: 'object', properties: { task: { type: 'string' }, detail: { type: 'string' } }, required: ['task', 'detail'] },
    },
    error: { type: 'string' },
  },
  required: ['done', 'ready', 'busy'],
}
const RESULT = {
  type: 'object',
  properties: {
    task: { type: 'string' },
    status: { type: 'string', enum: ['done', 'in_progress', 'blocked'] },
    gatePassed: { type: 'boolean' },
    summary: { type: 'string' },
    commits: { type: 'string' },
    gateTail: { type: 'string' },
    blocker: { type: 'string' },
    deviations: { type: 'string' },
    wrongTests: { type: 'string' },
    outsideFiles: { type: 'string' },
  },
  required: ['task', 'status', 'gatePassed', 'summary'],
}
const MERGE = {
  type: 'object',
  properties: {
    merged: { type: 'boolean' },
    conflicts: { type: 'string', description: 'files that conflicted and how they were resolved, or empty' },
    error: { type: 'string', description: 'why it did not merge, including a refused/blocked command' },
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

const GIT_NOTE = 'Run git as "command git ..." (a shell hook may rewrite plain git) and prefer "command git -C <dir>" with absolute paths over compound cd commands. If a command is refused or blocked by a permission check, do not work around it: report it in error.'

function limiter(max) {
  if (!max || max < 1) return fn => fn()
  let active = 0
  const queue = []
  const next = () => {
    if (active >= max || !queue.length) return
    active++
    const { fn, resolve } = queue.shift()
    fn().then(resolve, () => resolve(null)).finally(() => { active--; next() })
  }
  return fn => new Promise(resolve => { queue.push({ fn, resolve }); next() })
}
const limit = limiter(A.maxConcurrent)

const setupPrompt = (w, nums) => `Mechanical setup for wave ${w} of spec ${SPEC}. Do exactly this, nothing else. ${GIT_NOTE}
1. Run, from ${TARGET}:  ck waves ${SPEC} --json
   Its "done" list holds the tasks already Done on ${INTO}. Of these tasks: ${nums.join(', ')}, the ones in "done" go in your "done" field.
2. For the remaining ones run, from ${ROOT}:  ck worktree create ${SPEC} <their numbers> --install --base ${INTO} --json
   Each entry's "state": CREATED or EXISTS means ready; BUSY means another agent or uncommitted work owns that worktree (put it in "busy" with its detail, do not touch it); ERROR is a failed install (put it in "busy" too).
Return done, ready, busy, and error only if a command itself failed to run.`

const taskPrompt = n => `Your task: Task ${n} of spec ${SPEC}${TITLES[n] ? ` ("${TITLES[n]}")` : ''}.
Worktree: ${WT(n)} (branch ${BR(n)}). Target branch for the final merge: ${INTO}.
First read your operating brief at ${BRIEF} and follow it exactly: claim the worktree, implement per your track, get "ck gate ${SPEC} --task ${n}" passing, merge ${INTO} into your branch before finishing, release the worktree.
${GIT_NOTE}
Return the schema fields with task "${n}". gatePassed is true only if your final "ck gate ${SPEC} --task ${n}" run exited 0.`

const retryPrompt = (n, prev, attempt) => `Continue Task ${n} of spec ${SPEC} (attempt ${attempt + 1}). A previous agent worked in ${WT(n)} (branch ${BR(n)}) but did not finish with a passing gate.
Its report: ${trim(prev || { note: 'the agent died without a report' })}
Read your operating brief at ${BRIEF} and follow it, claiming with "ck worktree claim ${SPEC} ${n} --takeover" since the previous agent is gone. Inspect the state ("command git -C ${WT(n)} log --oneline -10", "command git -C ${WT(n)} status"), finish the task, and get "ck gate ${SPEC} --task ${n}" to pass. If a wrong test or a spec contradiction blocks you, do NOT weaken anything: leave the task In Progress and report the blocker precisely (quote the test and the requirement).
Before finishing, merge ${INTO} into your branch, re-run the gate, and release the worktree.
${GIT_NOTE}
Return the schema fields with task "${n}".`

const mergePrompt = n => `Merge step for Task ${n} of spec ${SPEC}. ${GIT_NOTE}
1. From ${ROOT} run:  ck worktree merge ${SPEC} ${n} --into ${INTO} --json
   - state MERGED: success.
   - state SKIP (no commits ahead): return merged=false, error "no commits".
   - state BUSY: the worktree is still claimed. Run "ck worktree release ${SPEC} ${n}" once, then retry the merge once.
   - state CONFLICT: the merge was already aborted on ${INTO}. Resolve it in the TASK WORKTREE, never in ${TARGET}:
       command git -C ${WT(n)} merge ${INTO}
     Resolve each conflicted file keeping both sides' intent: tasks.md, keep both sides' task sections; dependency manifests, take the union and re-lock (e.g. "uv lock"); test files, keep every test and assertion from both sides and never weaken one.
     In the worktree, the test collection and "ck gate ${SPEC} --task ${n}" must pass. Commit the merge, then run the ck worktree merge command again.
   - state ERROR: return merged=false with the detail.
2. After MERGED, refresh dependencies in ${TARGET} if the project has an install step (e.g. "uv sync -q" from ${TARGET}).
Never edit files in ${TARGET} directly, never force anything, never touch other task branches.
Return merged, conflicts, error.`

const verifyPrompt = w => `Verification after wave ${w} of spec ${SPEC}. In ${TARGET} (branch ${INTO}):
1. Refresh dependencies if the project has an install step (e.g. "uv sync -q").
2. Run:  ck gate ${SPEC}
   It checks every Done task: acceptance boxes, the tests that should already pass, test collection, and that no test was weakened.
Do not modify anything. Return green=true only if "ck gate ${SPEC}" exited 0; otherwise put the failing checks and the relevant output tail in failing.`

const fixPrompt = (w, v) => `${INTO} is red after wave ${w} of spec ${SPEC}. Failure: ${trim(v, 4000)}
Fix it without weakening any test and without editing requirements.md. ${GIT_NOTE}
1. From ${ROOT}: ck worktree create ${SPEC} wave${w}-fix --install --base ${INTO}   (worktree ${WT(`wave${w}-fix`)}). Work only there.
2. Diagnose with the failing check, fix the code (or a clearly broken fixture or harness), and confirm "ck gate ${SPEC}" passes in the fix worktree.
3. Commit with a message starting "fix(wave ${w}):", then from ${ROOT}: ck worktree merge ${SPEC} wave${w}-fix --into ${INTO}
If the only way to green is to weaken a test or change a requirement, stop and explain precisely instead.
Return green=true only if ${INTO} is green after your merge, else failing with the reason.`

const failed = new Set()   // tasks that did not finish or merge
const report = []

for (let i = START - 1; i < WAVES.length; i++) {
  const w = i + 1
  const P = `Wave ${w}`
  phase(P)

  const blocked = WAVES[i].filter(n => (DEPS[n] || []).some(d => failed.has(d)))
  blocked.forEach(n => failed.add(n))
  const candidates = WAVES[i].filter(n => !blocked.includes(n))
  if (blocked.length) log(`${P}: skipping Task ${blocked.join(', ')} (a dependency did not merge)`)
  if (!candidates.length) { report.push({ wave: w, blocked }); continue }

  const setup = await agent(setupPrompt(w, candidates), { label: `setup wave ${w}`, phase: P, schema: SETUP, effort: 'low' })
  if (!setup || setup.error) {
    report.push({ wave: w, halted: true, reason: 'setup failed', setup })
    log(`${P}: setup failed, halting`)
    return { halted: true, atWave: w, into: INTO, failedTasks: [...failed], report }
  }
  const done = candidates.filter(n => setup.done.includes(n))
  const busy = setup.busy || []
  busy.forEach(b => failed.add(String(b.task)))
  const todo = candidates.filter(n => !done.includes(n) && setup.ready.includes(n))
  log(`${P}: ${todo.length} to run${done.length ? `, ${done.length} already Done` : ''}${busy.length ? `, ${busy.length} busy` : ''}`)
  if (!todo.length) {
    // Nothing merges in this wave (resume / partial rerun), so the target is unchanged.
    report.push({ wave: w, alreadyDone: done, busy, blocked })
    if (busy.length && !CONTINUE) {
      log(`${P}: halting (busy worktrees: Task ${busy.map(b => b.task).join(', ')})`)
      return { halted: true, atWave: w, into: INTO, failedTasks: [...failed], report }
    }
    continue
  }

  let mergeChain = Promise.resolve()
  const results = await parallel(todo.map(n => async () => {
    let r = await limit(() => agent(taskPrompt(n), { label: label(n), phase: P, schema: RESULT }))
    for (let attempt = 1; attempt <= MAX_RETRIES && !ok(r) && !(r && r.blocker && /busy/i.test(r.blocker)); attempt++) {
      log(`Task ${n}: gate not passed, retry ${attempt}/${MAX_RETRIES}`)
      r = await limit(() => agent(retryPrompt(n, r, attempt), { label: `${label(n)} retry ${attempt}`, phase: P, schema: RESULT }))
    }
    if (!ok(r)) { failed.add(n); return { task: n, ok: false, result: r } }

    // Merge as soon as this task is done, one merge at a time.
    const link = mergeChain.then(() => agent(mergePrompt(n), { label: `merge ${n}`, phase: P, schema: MERGE, effort: 'low' }))
    mergeChain = link.catch(() => null)
    const m = await link.catch(() => null)
    if (!m || !m.merged) { failed.add(n); return { task: n, ok: false, result: r, merge: m } }
    log(`Task ${n}: done and merged into ${INTO}`)
    return { task: n, ok: true, result: r, merge: m }
  }))
  await mergeChain

  const finished = results.map((x, k) => x || { task: todo[k], ok: false, result: null })
  const waveFailed = finished.filter(x => !x.ok).map(x => x.task).concat(busy.map(b => String(b.task)))

  let verify = await agent(verifyPrompt(w), { label: `verify wave ${w}`, phase: P, schema: VERIFY })
  let fix = null
  if (!verify || !verify.green) {
    log(`${P}: ${INTO} is red, one fix attempt`)
    fix = await agent(fixPrompt(w, verify), { label: `fix wave ${w}`, phase: P, schema: VERIFY })
    verify = await agent(verifyPrompt(w), { label: `re-verify wave ${w}`, phase: P, schema: VERIFY })
  }

  report.push({
    wave: w,
    alreadyDone: done,
    busy,
    blocked,
    results: finished.map(x => ({
      task: x.task,
      ok: x.ok,
      status: x.result && x.result.status,
      summary: x.result && x.result.summary,
      blocker: x.result && x.result.blocker,
      wrongTests: x.result && x.result.wrongTests,
      outsideFiles: x.result && x.result.outsideFiles,
      merge: x.merge && (x.merge.merged ? 'merged' : x.merge.error || 'not merged'),
    })),
    verify,
    fix,
  })

  const green = verify && verify.green
  if (!green || (waveFailed.length && !CONTINUE)) {
    log(`${P}: halting (${waveFailed.length} task(s) not done/merged; ${INTO} ${green ? 'green' : 'red'})`)
    return { halted: true, atWave: w, into: INTO, failedTasks: [...failed], report }
  }
  log(`${P}: ${finished.filter(x => x.ok).length} merged, ${INTO} green${waveFailed.length ? `; continuing past Task ${waveFailed.join(', ')}` : ''}`)
}

return { halted: false, into: INTO, failedTasks: [...failed], report }
