In all interactions, plans and commit messages, be extremely concise and sacrifice for the sake of concision. Do not apply this for code written.

Never add "Co-Authored-By: Claude" line to any commit message.

List any unresolved questions at the end, if any

## Project

**rangectl** — Rapid Automated Network Generation and Environment Control. Python SDK for declarative VM testbed orchestration on libvirt/QEMU. Single-host, deployable on any Ubuntu box.

## Key Documents

- `agents/docs/development-rules.md` — TDD workflow, test commands, code standards
- `agents/docs/ec2-usage.md` — EC2 instance management for integration tests
- `agents/docs/TEAM-LEAD-AGENT-GUIDE.md` — orchestrator workflow for phased development
- `scratch/issues/20260527-1-vm-testbed-platform-design.md` — implementation phases (THE PLAN)
- `scratch/issues/20260527-2-requirements-and-design-decisions.md` — requirements (R1-R15) and design decisions (D1-D23)
- `scratch/issues/20260527-3-sdk-api-reference.md` — SDK API surface
- `scratch/issues/20260527-4-testing-strategy.md` — test topologies, gate definitions, MockBackend

## Testing

- **Gate 1**: `pytest tests/unit` — runs anywhere, uses MockBackend + in-memory SQLite
- **Gate 2**: `pytest tests/integration` — runs on EC2 (KVM required), tests real VMs/bridges/SSH
- Gate 1 gates every commit. Gate 2 gates phase completion.
- Test topologies (Topo 1-6) define integration pass criteria per phase.

## Scratch Area Structure

### `/scratch/scripts/`
All debugging scripts, one-off utilities, and diagnostic tools.

### `/scratch/issues/`
**All work must be grounded in issues here.**

When creating new issues:
- **Run `~/.claude/scripts/next-prefix scratch/issues` to get the filename prefix** (e.g., `20260213-1`), then append `-feature-name.md`
- Update continuously as work progresses
- Include: goal, steps, progress, blockers, resolution, related issues
- **Bidirectional linking**: When linking to a parent/related issue, also update that issue to link back here

### Issue Template
```markdown
# [Feature/Bug]: Brief Title
**Created**: YYYY-MM-DD
**Status**: In Progress | Blocked | Complete

## Related Issues
- **Parent**: `ISSUE.md` - (update parent to link back here)
- **Related**: `ISSUE.md` - quick description


## Goal
[Clear objective]

## Steps
- [ ] Step 1
- [ ] Step 2

## Test Runs
[Track each Playwright test run with screenshots]
- `screenshots/run-YYYYMMDD-HHMMSS/` - Description of test, results

## Progress Log
[Continuous updates]

## Resolution
[Final outcome]
```

<reminder>
Premature optimization and complexity is highly undesirable
</reminder>


## Stubbing functions
- Function stubbing means implementing skeletal versions of functions that establish the control flow of your application, accompanied by log statements to track execution paths. This approach allows you to map out the architecture and interaction between components before committing to full implementations.

### Stric Rules for debugging

Take a step back and slow down. Isolate the root cause. Do not make code changes other then adding logs or
writing debug tests in scratch. Identidy the root cause. Then implement the fix. create an issue in scratch/issues. This should be continuously updated throughout the debugging process. The issue is the central place for information validation results, fix summaries, list of test files created during debuging.

    📊 Success Criteria

    After running diagnostics, we will have:
    1. ✅ Clear evidence of EXACTLY where the failure occurs
    2. ✅ Logs showing daemon startup sequence
    3. ✅ Confirmation of which hypothesis is correct
    4. ✅ Specific fix identified (not guessed)

    🚫 What We Will NOT Do

    - ❌ Make code changes before understanding root cause
    - ❌ Try multiple fixes hoping one works
    - ❌ Add more complexity without evidence
    - ❌ Assume anything without checking

I want a well-designed prototype. Do not perceive my aversion to premature complexity as a desire for sloppy code—I value clean, maintainable code that serves the core purpose without over-engineering.

### Avoid
- Premature abstractions
- Over-engineered architecture
- Features not essential to the core value proposition
- Complex dependencies when simpler alternatives exist
- Optimization before identifying actual bottlenecks


### UI development
Use vanilla javascript and CSS for UIs. I don't want complexity here until i have follow defined user work flows and have an end to end application working. Then we can we factor to add frameworks and complexity.

## Code Quality Standards

- Write clear, readable code with descriptive variable names
- Include basic error handling where it matters
- Add comments for business logic, not implementation details
- Keep functions focused on single responsibilities
- Structure code for easy modification, not premature scalability

<reminder>
Make sure to work from the current issue at hand in `scratch/issues/`. Update it as you go
</reminder>


<reminder>
Function stubbing = develop implementing module skeleton
</reminder>

<reminder>
Make sure to follow the strict debugging rules. Always slow down to identify the root cause or causes before generating a fix
</reminder>


<ux>
Each interaction = micro-win, not hurdle. Momentum over cognitive load.
</ux>
