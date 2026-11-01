// Plan-agent shim for the fallback-spec fixture (deterministic-plan Story 6).
//
// Wraps an agent function (the fake project's `agent` in CI, the real runtime's in the
// manual re-run) and, in `forced-inconsistent` mode, makes the first two plan answers
// return a ready list contradicting their deps (an unknown id), so the scheduler must
// retry once, fall back deterministically, complete, and report degraded. In `healthy`
// mode every answer passes through untouched. The mode comes from the constructor arg
// or `FALLBACK_SPEC_PLAN_MODE` (see ck.json).
export const MODES = ['healthy', 'forced-inconsistent']

export function shimPlanAgent(agent, mode = process.env.FALLBACK_SPEC_PLAN_MODE || 'healthy') {
  if (!MODES.includes(mode)) throw new Error(`unknown plan shim mode: ${mode} (expected one of ${MODES.join(', ')})`)
  let plans = 0
  return async (prompt, opts) => {
    const answer = await agent(prompt, opts)
    const label = opts && typeof opts.label === 'string' ? opts.label : ''
    if (mode === 'forced-inconsistent' && label.startsWith('plan') && plans < 2) {
      plans++
      return { ...answer, ready: ['999'] }
    }
    return answer
  }
}
