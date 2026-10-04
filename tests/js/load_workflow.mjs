// Load a Workflow script and run it with stubbed agent/parallel/pipeline/phase/log.
import fs from 'node:fs'

export async function runWorkflow(path, { agent, args }) {
  const src = fs.readFileSync(path, 'utf8').replace(/^export const meta/m, 'const meta')
  const logs = []
  const parallel = thunks => Promise.all(thunks.map(t => t().catch(() => null)))
  const pipeline = (items, ...stages) =>
    Promise.all(items.map(async (item, i) => {
      let r
      for (const [n, stage] of stages.entries()) {
        try { r = await stage(n === 0 ? item : r, item, i) } catch { return null }
      }
      return r
    }))
  const fn = new Function('agent', 'pipeline', 'parallel', 'phase', 'log', 'args', `return (async () => {${src}})()`)
  const result = await fn(agent, pipeline, parallel, () => {}, m => logs.push(m), args)
  return { result, logs }
}

export const sleep = ms => new Promise(r => setTimeout(r, ms))
