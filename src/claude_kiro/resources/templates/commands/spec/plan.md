---
description: Research best practices, analyze approaches, and create a decision document before writing requirements
argument-hint: [feature-description]
allowed-tools: Read, Write, Grep, Glob, WebSearch, WebFetch, AskUserQuestion, ExitPlanMode
---

Research and plan feature: $ARGUMENTS

# Spec-Plan: Pre-Requirements Research & Decision Framework

Before we write requirements, you should research the problem space thoroughly and create a structured decision document for me to review.

**CRITICAL: This is a research and discussion phase. The goal is informed decision-making BEFORE committing to requirements. You MUST use ExitPlanMode after presenting research findings to discuss direction with me.**

---

## Phase 1: Understand the Problem Space

### 1.1 Clarify Scope

Before researching, make sure you understand what we're building:

1. **Core problem** - What user need does this address?
2. **Scope boundaries** - What is explicitly in/out?
3. **Existing work** - Check `.claude/specs/` for related specs
4. **Codebase patterns** - Search for similar functionality already in the project

If the feature description is ambiguous, use AskUserQuestion to clarify before researching. Better to spend time here than research the wrong thing.

### 1.2 External Research

Use WebSearch and WebFetch to find:

**Best practices and patterns:**
```
"best practices" [feature domain] [current year]
"design patterns" [feature type] [language/framework]
"architecture" [feature domain] production
```

**Real-world implementations:**
```
"how to implement" [feature type] [technology]
[feature domain] "open source" example
[feature type] tutorial [framework]
```

**Known pitfalls and edge cases:**
```
"common mistakes" [feature type]
"pitfalls" [feature domain]
[feature type] "lessons learned"
```

**Run at least 3-5 searches.** Follow promising results with WebFetch to get details. Cross-reference claims across multiple sources.

### 1.3 Evaluate Sources

For each source you use:
- **Credibility** - official docs > established engineering blogs > tutorials > forum posts
- **Recency** - prefer sources from the last 2 years
- **Relevance** - does it match our specific context (language, scale, constraints)?
- **Extract** the key insight, not just the URL

---

## Phase 2: Analyze Solution Approaches

### 2.1 Identify 2-4 Distinct Approaches

Based on your research, identify genuinely different approaches. Not minor variations - fundamentally different ways to solve the problem.

For each approach:
- **Name** - a clear, descriptive label
- **How it works** - brief technical explanation
- **Where it shines** - what it handles well
- **Where it struggles** - what it doesn't handle well
- **Real-world usage** - who uses this approach and at what scale

### 2.2 Comparative Analysis

Build a comparison table:

| Criteria | Approach A | Approach B | Approach C |
|----------|-----------|-----------|-----------|
| Implementation complexity | | | |
| Flexibility / extensibility | | | |
| Maintainability | | | |
| Performance characteristics | | | |
| Learning curve | | | |
| Integration with our codebase | | | |
| Risk level | | | |

### 2.3 Complexity Assessment

Rate overall feature complexity (Low / Medium / High / Very High) and identify specific complexity drivers:
- Integration points
- State management
- Concurrency / async
- Security surface
- Performance constraints
- Migration / backwards compatibility

---

## Phase 3: Create the Decision Document

Write `.claude/specs/[feature-name]/PLAN.md` with this structure:

```markdown
# Plan: [Feature Name]

**Created:** [date]
**Status:** Pending Decision

## Problem Statement
[What problem are we solving and for whom?]

## Research Summary

### Sources Consulted
- [Source 1](url) - key insight
- [Source 2](url) - key insight
- [Source 3](url) - key insight

### Best Practices Found
- [Practice 1] - [source, context]
- [Practice 2] - [source, context]

### Patterns to Avoid
- [Anti-pattern 1] - [why, source]
- [Anti-pattern 2] - [why, source]

## Solution Options

### Option A: [Name]

**Description:** [How it works]

**Pros:**
- [Pro 1]
- [Pro 2]

**Cons:**
- [Con 1]
- [Con 2]

**Complexity:** Low/Medium/High
**Precedent:** [Who uses this, at what scale]

---

### Option B: [Name]
[same structure]

---

## Comparison Matrix

| Criteria | Option A | Option B | Option C |
|----------|----------|----------|----------|
| ... | ... | ... | ... |

## Key Decisions

Decisions we need to make before writing requirements:

1. **[Decision question]**
   - Option X: [rationale]
   - Option Y: [rationale]
   - Recommendation: [which and why]

2. **[Decision question]**
   - [same structure]

## Open Questions

- [ ] [Question needing user input]
- [ ] [Technical unknown to resolve]

## Recommended Direction

**Approach:** [recommended option]

**Rationale:**
- [Reason 1]
- [Reason 2]

**Key risks and mitigations:**
- [Risk] -> [mitigation]

## Decisions Log

| # | Decision | Choice | Rationale | Date |
|---|----------|--------|-----------|------|
| | | | | |

_Populated after discussion with user._
```

---

## Phase 4: Present and Discuss

After writing PLAN.md, use ExitPlanMode to present your findings. Structure your presentation as:

1. **What I found** - 3-5 key research insights
2. **Options on the table** - brief summary of each approach
3. **My recommendation** - which option and why
4. **What I need from you** - specific decisions/questions

Then discuss with me. Use AskUserQuestion if you need clarification on priorities, constraints, or preferences.

### After Discussion

Once we've made decisions together, update PLAN.md:
- Fill in the **Decisions Log** table with what was decided and why
- Update **Status** to "Decisions Made"
- Mark resolved items in **Open Questions**

Then tell me: "Plan complete. Run `/spec:create [feature-name]` to write requirements based on our decisions."

---

## Workflow Summary

1. **Understand** - Clarify scope, check existing work
2. **Research** - WebSearch/WebFetch for best practices and real examples
3. **Analyze** - Identify approaches, compare tradeoffs
4. **Document** - Write structured PLAN.md
5. **Discuss** - ExitPlanMode to review findings with me
6. **Decide** - Record decisions and rationale
7. **Hand off** - PLAN.md feeds into `/spec:create`

**Output:** `.claude/specs/[feature-name]/PLAN.md`
