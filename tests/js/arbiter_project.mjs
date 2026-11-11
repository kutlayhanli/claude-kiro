// A real temporary git project for the migrated test-repair scenarios (run-principles Task 102): `specs/PRINCIPLES.md`,
// the scenario's spec and an integration worktree, so the fake runtime's `arbiter-` thin steps (`ck arbiter open`,
// `settle`, `record`) run the real CLI through `execCk`. Mirrors tests/arbiter_helpers.py's `arbiter_project`.
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.join(here, '../..')
const PYTHON = path.join(repo, '.venv/bin/python')
const CK = [PYTHON, '-m', 'claude_kiro.cli']

const PRINCIPLES = `# Principles

## Principles

- **test-intent:** a test states what the requirement says; repair the test, never bend the requirement to it.
- **remove-first:** remove before you optimize.
- **ask-on-reserved:** a reserved scope is decided by me, not by the arbiter.
`
const PLAN = '# Plan: Demo\n\n## Principles\n\n- **Nothing to add:** the project defaults apply.\n'
const REQUIREMENTS = `# Requirements: Demo

## User Stories

### Story 1: Greeting

**As a** user **I want** a greeting **So that** I feel welcome.

**Without it:** the app is silent and users cannot tell it started.

**Acceptance Criteria:**

- 1.1 WHEN the app starts, THE SYSTEM SHALL print a greeting.
`
const taskBlock = n => `### Task ${n}: Work ${n}
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** Work ${n}.
**Files:**
- \`src/work${n}.py\` - the work
**Verify:** \`uv run pytest -q tests/test_work${n}.py\`
**Dependencies:** None

---
`
const git = (cwd, ...args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: 'pipe' })

// `tasks`: how many task blocks tasks.md holds (numbered from 1). Returns {root, target, execCk, args}.
export function arbiterProject({ spec = 'demo', tasks = 6 } = {}) {
  const root = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'ck-arbiter-js-')), 'repo')
  const specs = path.join(root, 'specs')
  const dir = path.join(specs, spec)
  fs.mkdirSync(dir, { recursive: true })
  git(root, 'init', '-q', '-b', 'main')
  git(root, 'config', 'user.name', 'Test User')
  git(root, 'config', 'user.email', 'test@example.com')
  const ckJson = { verify: ['python -c pass'], guard: { tests: 'ask', requirements: 'ask', test_plan: 'ask' }, base: null }
  fs.writeFileSync(path.join(specs, 'ck.json'), JSON.stringify(ckJson, null, 2) + '\n')
  fs.writeFileSync(path.join(specs, 'PRINCIPLES.md'), PRINCIPLES)
  fs.writeFileSync(path.join(dir, 'PLAN.md'), PLAN)
  fs.writeFileSync(path.join(dir, 'requirements.md'), REQUIREMENTS)
  fs.writeFileSync(path.join(dir, 'design.md'), '# Design: Demo\n\nA greeting.\n')
  fs.writeFileSync(path.join(dir, 'test-plan.md'), '# Test Plan: Demo\n\n| TC-1 | 1.1 | I | greeting printed | CLI | tests/test_greet.py |\n')
  const blocks = Array.from({ length: tasks }, (_, i) => taskBlock(i + 1)).join('\n')
  fs.writeFileSync(path.join(dir, 'tasks.md'), `# Implementation Tasks: Demo\n\n**Status:** Not Started\n\n## Task Breakdown\n\n${blocks}`)
  fs.writeFileSync(path.join(root, '.gitignore'), '.ck/\n.claude/worktrees/\n__pycache__/\n')
  git(root, 'add', '-A')
  git(root, 'commit', '-qm', 'init')
  execFileSync(CK[0], [...CK.slice(1), 'worktree', 'integration', spec, '--base', 'main'], { cwd: root, encoding: 'utf8', stdio: 'pipe' })
  const target = path.join(root, '.ck', 'worktrees', `${spec}-integration`)
  if (!fs.existsSync(target)) throw new Error(`no integration worktree at ${target}`)
  return { root, target, execCk: { cmd: CK, cwd: root }, args: { spec, root, into: `integrate/${spec}`, targetDir: target } }
}

// The `arbiter task <n>` answer that rules a conflict `test_repair`: the ruling's change and why are the conflict's fix and reason.
export function repairRuling(n, c) {
  return {
    claim: `${n}|test:${c.file}::${c.test}`, status: 'ruled', outcome: 'test_repair', principle: 'project:test-intent',
    reasoning: 'the test asserts a value no correct code returns; its intent stands, only the value moves',
    testRepair: { file: c.file, test: c.test, change: c.fix, why: c.reason },
  }
}
