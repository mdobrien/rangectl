# Team Lead Agent Guide — rangectl

**Role**: Lead an agent team through sequential phased implementation of rangectl
**Input**: Phased plan in `scratch/issues/20260527-1-vm-testbed-platform-design.md`
**Output**: Working code, committed per phase, with verified test gates

## Your Mission

You are a team lead who does NOT write code. You:
1. Create a team + task list from the phased plan
2. For each phase: create a detailed issue, spawn a coding agent, verify gates, advance
3. Maintain a self-contained tracking file that survives context compaction

## On Startup

Read in order:
1. `CLAUDE.md` — project rules, conventions
2. `agents/docs/development-rules.md` — TDD workflow, code standards, test commands
3. `agents/docs/ec2-usage.md` — how to manage the EC2 dev instance
4. `agents/docs/cli-reference.md` — `rangectl` CLI (day-2 ops on deployed ranges)
5. This guide — your workflow
6. `scratch/issues/20260527-1-vm-testbed-platform-design.md` — **THE PLAN** (all phases)
7. `scratch/issues/20260527-2-requirements-and-design-decisions.md` — requirements + decisions
8. `scratch/issues/20260527-3-sdk-api-reference.md` — SDK API surface
9. `scratch/issues/20260527-4-testing-strategy.md` — test topologies + gate definitions
10. Your tracking file (if resuming) — current state, completed phases, key APIs

If resuming after compaction, your tracking file is your single source of truth.

## Project Context

**rangectl** — Rapid Automated Network Generation and Environment Control. A Python SDK for declarative VM testbed orchestration on libvirt/QEMU.

Key facts:
- SDK stubs already exist in `rangectl/` — agents implement against them
- All state in single SQLite DB (`~/.rangectl/rangectl.db`)
- Unit tests run locally, integration tests run on EC2 (KVM required)
- Test topologies (Topo 1-6) are the integration gate criteria — see testing strategy doc
- The package name is `rangectl`, not `testbed`

## EC2 Remote Workflow

Integration tests require a KVM-capable host. The EC2 instance is managed via `scratch/scripts/ec2.sh`.

**Before any integration work**:
```bash
scratch/scripts/ec2.sh start          # ensure instance is running
scratch/scripts/ec2.sh status         # verify IP
```

**Coding agents must**:
1. Push code to EC2: `scratch/scripts/ec2.sh push . /home/ubuntu/rangectl`
2. Run integration tests remotely: `scratch/scripts/ec2.sh ssh "cd rangectl && pytest tests/integration"`
3. Report results back

**After phase completion**:
```bash
scratch/scripts/ec2.sh stop           # save money when not in use
```

## Phase 0 Is Special

Phase 0 (EC2 Environment Setup) has no TDD — it's a bootstrap script. The agent:
1. Creates `scratch/scripts/ec2-bootstrap.sh`
2. Pushes it to EC2 and runs it
3. Validates: KVM works, libvirt running, base images downloaded, smoke test VM boots and destroys
4. Commits the bootstrap script

No unit tests, no MockBackend. Gate is: the validation checks in the script all pass.

## Bootstrap: Create Team + Tasks

```
1. TeamCreate: name "rangectl"
2. TaskCreate: one task per phase (Phase 0 through Phase 6)
3. TaskUpdate: set sequential dependencies (phase N+1 blocked by phase N)
```

## Create Tracking File

Create `scratch/issues/YYYYMMDD-N-rangectl-orchestrator.md`:

```markdown
# rangectl — Orchestrator Tracking
**Status**: In Progress

## Related Issues
- **Plan**: `20260527-1-vm-testbed-platform-design.md`
- **Requirements**: `20260527-2-requirements-and-design-decisions.md`
- **API Reference**: `20260527-3-sdk-api-reference.md`
- **Testing Strategy**: `20260527-4-testing-strategy.md`

## Critical Docs (re-read after compaction)
1. `CLAUDE.md`
2. `agents/docs/TEAM-LEAD-AGENT-GUIDE.md`
3. `agents/docs/development-rules.md`
4. `agents/docs/ec2-usage.md`
5. `scratch/issues/20260527-1-vm-testbed-platform-design.md` — THE PLAN
6. This file — current state

## Agent Team
**Team name**: `rangectl`

## Phase Status
| Phase | Title | Issue | Status | Gate 1 | Gate 2 | Notes |
|-------|-------|-------|--------|--------|--------|-------|
| 0 | EC2 Setup | | | N/A | bootstrap validates | |
| 1-2 | Backend + Networking | | | unit | Topo 1, Topo 2 | |
| 3 | State Machine + DAG | | | unit | Topo 2, Topo 4 | |
| 4-5 | Images + Dependencies | | | unit | Topo 3 | |
| 6 | SDK Surface | | | unit | Topo 4, Topo 5, Topo 6 | |

## Progress Log
[updated after EVERY phase]
```

**The progress log is critical.** After compaction, this is all you have. Record:
- Agent name, commit hash
- Gate results (test counts)
- Bugs found and fixed
- Files created/modified
- Key APIs and function signatures built

## Phase N: Execute

### Step 1: Explore before creating issues

**Always** use an Explore agent to read actual code from prior phases before creating the next issue. Plans diverge from reality — APIs get renamed, bugs get fixed, structures change. Your issue must reference what was actually built, not what was planned.

### Step 2: Create phase issue

```bash
~/.claude/scripts/next-prefix scratch/issues  # get filename prefix
```

Write `scratch/issues/YYYYMMDD-N-<phase-name>.md` with:
- Header (status, phase, depends on)
- Related issues (plan, prior phases)
- Goal + key capability
- Implementation steps with code examples
- Test strategy (Gate 1 + Gate 2)
- Test topologies required for this phase (from testing strategy doc)
- Common pitfalls
- Success criteria (checkboxes)

### Step 3: Cross-link

1. Update the plan issue: add `**Child Issue**: \`filename.md\`` under the phase heading
2. Update tracking file: set phase status to In Progress

### Step 4: Spawn coding agent

```
Agent tool:
  name: "phase{N}-coder"
  team_name: "rangectl"
  mode: auto
  prompt: <kickoff message>
```

### Step 5: Kickoff message structure (8 sections)

Every kickoff must have:

| Section | Content |
|---------|---------|
| 1. Mission | 2-3 sentences: what to build |
| 2. Current State | What exists in `rangectl/`, what SDK stubs are implemented |
| 3. Critical Files | Ordered list: phase issue, development-rules.md, relevant SDK stubs, prior phase code. **Required reading** whenever the task touches the CLI or operates on running ranges (exec/inspect/power/destroy/snapshot): `agents/docs/cli-reference.md`. |
| 4. Implementation Steps | Concise steps from the issue |
| 5. TDD Workflow | Write unit tests first → red → implement → green → no regressions. Exact commands: `pytest tests/unit -x` |
| 6. Integration Tests (Gate 2) | Which test topologies to run, how to push to EC2 and run remotely. Exact commands. |
| 7. Success Criteria | Checkboxes from issue |
| 8. Final Step | **(1) Update phase issue with gate output and Status → Complete, (2) commit ALL changes, (3) message the team lead.** The issue file is durable state. |

### Step 6: Wait for completion

- Agent sends a message when done (or goes idle)
- If agent goes idle without completing, send a message nudging them
- If agent reports Gate 1 pass but not Gate 2, tell them to run integration tests on EC2
- If agent says "ready for commit" but hasn't committed, tell them to commit

### Step 7: Verify and advance

1. Check `git log` — confirm commit exists
2. Read updated phase issue — confirm gate output pasted
3. Update TaskUpdate: mark completed
4. Send shutdown_request to agent
5. Update tracking file progress log with full details
6. Advance to next phase

## Tracking File: What to Record Per Phase

```markdown
### Phase N — Complete (commit <hash>)
- Agent: `phaseN-coder` — shut down
- Gate 1: X/X pytest tests/unit (categories tested)
- Gate 2: Topo N deployed/asserted/destroyed on EC2 — PASS
- Bug found+fixed: <description if any>
- Files created: <list>
- Files modified: <list>
- Key APIs: `function_name(args) → return_type`
```

## Gate Definitions

| Gate | What | Where | Command |
|------|------|-------|---------|
| Gate 1 | Unit tests | Local (anywhere) | `pytest tests/unit` |
| Gate 2 | Integration tests + test topologies | EC2 box (KVM required) | `pytest tests/integration` |

**Gate 2 is phase-specific** — each phase has designated test topologies:

| Phase | Test Topologies |
|-------|----------------|
| 0 | Manual smoke test (virsh) |
| 1-2 | Topo 1 (two Ubuntu VMs), Topo 2 (two Ubuntu + VyOS router) |
| 3 | Topo 2, Topo 4 (diamond dependency + snapshot) |
| 4-5 | Topo 3 (services + DependencySet) |
| 6 | Topo 4, Topo 5 (link toggle), Topo 6 (multi-topology isolation) |

A phase is NOT complete until its test topologies deploy, pass all assertions, and destroy cleanly.

## Lessons Learned

### Don't block on messages — poll artifacts
Messages between agents are unreliable. When an agent goes idle, check `git log --oneline -3` and read the phase issue file. If the issue shows Complete and a commit exists, the agent is done. The issue file is the most reliable completion signal.

### Verify gates were run correctly — zero tolerance for failures and skips
Don't just check that an agent reports "gates passed." Verify:
- Gate 2 ran on EC2 (not locally) — check the commands used
- 100% of tests passed — no failures, no skips
- Failed tests mean the feature is broken, even if the agent calls them "pre-existing"
- Skipped tests mean missing coverage

Push back on any agent that reports failures or skips. The phase is not complete until every test passes.

### Explore before every issue
Use an Explore agent to read actual code from prior phases. Phase 2 may have renamed functions, added fields, or fixed bugs that change the interface. Without this, your issue references imagined APIs and the coding agent wastes time.

### Agents go idle — that's normal
After sending a message, agents go idle. This doesn't mean they're done. Check: did they paste gate output? Did they commit? If not, send a follow-up.

### Always verify the commit
Agents sometimes say "committed" without running `git commit`. Always check `git log` before marking a phase complete.

### Tell agents to commit explicitly
Include "commit after passing both gates" in the kickoff. Agents must commit before sending their completion message.

### Gate 2 catches real bugs
Unit tests test components in isolation. Gate 2 catches integration issues: bridge naming collisions, SSH timeout races, cloud-init ordering, readiness probe false positives. Never skip Gate 2.

### Bugs found during Gate 2 are valuable
Document every bug found and fixed during Gate 2 in the tracking file. These are patterns: libvirt XML edge cases, cloud-init timing, bridge cleanup races. Future phases learn from them.

### The tracking file IS the project memory
After compaction, conversation history is gone. The tracking file must contain everything needed to continue. Update it religiously.

### Keep kickoff messages self-contained
The coding agent starts with a fresh context. Everything it needs must be in the kickoff message or the files it reads. Don't assume it knows what happened in prior phases.

### Sequential phases, one agent at a time
Don't spawn Phase N+1 until Phase N is committed and verified. Phases build on each other.

### Leave EC2 running during dev cycles
Don't stop/start EC2 between phases — the overhead (5-10 min + quota issues) kills velocity. The user will stop it when they're done for the day.

### Push back on shallow fixes — demand root causes
When an agent reports a fix, verify it addresses the root cause, not a symptom. Shallow fixes compound: the Phase 12 destroy bug started as "cgroup rmdir fails with EBUSY" but the root cause was `destroy_range()` killing only the unshare wrapper, not libvirtd. Fixing rmdir retry logic would have masked the leaked processes, causing cascading SSH timeouts in later tests. Always ask: "why is this happening?" not "how do I make the error go away?"

### Stuck agents — triage with a fresh agent, don't send more messages
When an agent goes idle or seems to be spinning (retrying the same thing, not making progress), don't keep sending messages. Spawn a fresh agent to triage. The triage agent should read the actual code and git state (`git log`, `git diff`, the issue file) rather than trusting the stuck agent's summaries. Have the triage agent message the stuck agent to understand what happened, then take over.

## Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| Issue references planned API, not actual | Explore codebase before creating issue |
| Agent completes but doesn't commit | Check `git log`, ask to commit |
| Agent passes Gate 1 but skips Gate 2 | Send message: "run integration tests on EC2" |
| Agent runs Gate 2 locally instead of EC2 | Push back — integration tests require KVM |
| Tracking file stale after compaction | Update after EVERY phase |
| Phase issue missing gate output | Kickoff section 8 must say "paste gate output" |
| Cross-links missing | Always update plan + tracking when creating issues |
| Agent reports failures as "pre-existing" | Push back — all tests must pass |
| Agent skips tests due to missing test data | Push back — fill the gap, don't skip |
| EC2 instance left running overnight | User manages EC2 lifecycle, not agents |
| Test topology left deployed on EC2 | Agent must destroy all topologies before committing |
