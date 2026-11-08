// Scripted agents for the smoke tests: no LLM. A task agent does what the shipped prompt tells a real one to do:
// create and claim the worktree with the prompt's own commands, write `scenario.files[n]`, commit, run the prompt's gate
// command through the real ck, and return the RESULT built from ck's JSON. Every other label is answered by `fallback`
// (usually the fake project's agent).
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const tokens = text => [...text.trim().matchAll(/"([^"]*)"|(\S+)/g)].map(t => (t[1] !== undefined ? t[1] : t[2])).filter(t => t !== '')

// Every `ck <sub> ...` command the prompt writes out, as argv lists (without the leading `ck`), in prompt order.
export function promptCommands(prompt, subs) {
  const out = []
  const re = new RegExp(`\\bck ((?:-C \\S+ )?(?:${subs.join('|')})\\b[^\\n"\`]*)`, 'g')
  for (const m of prompt.matchAll(re)) out.push(tokens(m[1]))
  return out
}
const isSub = (argv, words) => { const r = argv[0] === '-C' ? argv.slice(2) : argv; return words.every((w, i) => r[i] === w) }

// Run `file args...` and resolve {code, stdout, stderr}.
function exec(bin, args, { cwd, env, external = p => p }) {
  return external(new Promise(resolve => {
    execFile(bin, args, { cwd, env: { ...process.env, ...(env || {}) }, encoding: 'utf8' }, (err, out, errOut) => {
      resolve({ code: err ? (typeof err.code === 'number' ? err.code : -1) : 0, stdout: out || '', stderr: errOut || '' })
    })
  }))
}

// realAgents({ ck: {cmd, cwd, env}, scenario: {files: {n: {relPath: content}}, message?}, fallback, external })
// -> { agent, runs }. `runs` records, per task attempt, the commands run: [{task, argv, code}].
export function realAgents({ ck, scenario = {}, fallback = null, external = p => p }) {
  const runs = []
  const runCk = async (task, argv) => {
    const [bin, ...pre] = ck.cmd
    const res = await exec(bin, [...pre, ...argv], { cwd: ck.cwd, env: { ...(ck.env || {}), CK_PROGRESS_TEST_KINDS: '1' }, external })
    runs.push({ task, argv, code: res.code })
    return res
  }
  const parse = t => { try { const v = JSON.parse(t); return v && typeof v === 'object' ? v : null } catch { return null } }
  const agent = async (prompt, opts) => {
    const l = (opts && opts.label) || ''
    const m = l.match(/^task (\d+)/)
    if (!m || / revise /.test(l)) {
      if (!fallback) throw new Error('realAgents: unexpected agent ' + l)
      return fallback(prompt, opts)
    }
    const n = m[1]
    const startedAt = new Date().toISOString().replace(/\.\d+Z$/, 'Z')
    const result = { task: n, status: 'in_progress', gatePassed: false, summary: '', commits: '', startedAt }
    // 1. create and claim the worktree
    const create = promptCommands(prompt, ['worktree create']).find(a => isSub(a, ['worktree', 'create']))
    let worktree = null
    if (create) {
      const res = await runCk(n, create)
      const entry = (Array.isArray(parse(res.stdout)) ? parse(res.stdout) : []).find(e => String(e.task) === n)
      if (entry) {
        result.worktree = { state: entry.state, detail: entry.detail ?? '' }
        worktree = entry.path ? path.resolve(ck.cwd || process.cwd(), entry.path) : null
        if (entry.state === 'BUSY' || entry.state === 'ERROR') {
          return { ...result, status: 'blocked', summary: `worktree ${entry.state}`, blocker: `worktree ${entry.state === 'BUSY' ? 'busy' : 'create failed'}: ${entry.detail ?? ''}`, finishedAt: new Date().toISOString().replace(/\.\d+Z$/, 'Z') }
        }
      }
    }
    const claim = promptCommands(prompt, ['worktree claim']).find(a => isSub(a, ['worktree', 'claim']))
    if (claim) {
      if (!worktree && claim[0] === '-C') worktree = claim[1]
      await runCk(n, claim)
    }
    // 2. write the scenario's files and commit them
    const files = (scenario.files && scenario.files[n]) || {}
    if (worktree && Object.keys(files).length) {
      for (const [rel, content] of Object.entries(files)) {
        fs.mkdirSync(path.dirname(path.join(worktree, rel)), { recursive: true })
        fs.writeFileSync(path.join(worktree, rel), content)
      }
      await exec('git', ['-C', worktree, 'add', '-A'], { env: ck.env, external })
      const c = await exec('git', ['-C', worktree, '-c', 'user.name=scripted', '-c', 'user.email=scripted@example.com', 'commit', '-q', '-m', scenario.message?.[n] ?? `task ${n}: scripted`], { env: ck.env, external })
      runs.push({ task: n, argv: ['git', 'commit'], code: c.code })
      const head = await exec('git', ['-C', worktree, 'rev-parse', '--short', 'HEAD'], { env: ck.env, external })
      result.commits = head.stdout.trim()
    }
    // 3. the prompt's gate command, through the real ck
    const gate = promptCommands(prompt, ['gate', 'worktree gate']).find(a => isSub(a, ['gate']))
    if (gate) {
      const res = await runCk(n, gate.includes('--json') ? gate : [...gate, '--json'])
      const json = parse(res.stdout)
      result.gatePassed = res.code === 0
      result.status = res.code === 0 ? 'done' : 'in_progress'
      result.summary = res.code === 0 ? 'gate passed' : 'gate failed'
      if (json && Array.isArray(json.failures)) result.failures = json.failures
    } else {
      result.summary = 'no gate command in the prompt'
    }
    return { ...result, finishedAt: new Date().toISOString().replace(/\.\d+Z$/, 'Z') }
  }
  return { agent, runs }
}
