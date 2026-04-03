# How to Run AI Agents — Practical Workflow Guide

## What "Agent A, B, C" Actually Means

When the project plan says "Agent A works on P1-V01, Agent B works on P1-V02",
it means: **you open two terminal windows and run two agent sessions in parallel,
each on its own Git branch, each with its own task prompt.**

You are NOT running some automated orchestration system. YOU are the orchestrator.
The agents are your tools. Here's exactly what happens:

## Step-by-Step: Running One Agent on One Task

### 1. Check what's ready
```bash
.agents/status.sh
```
This shows tasks with all dependencies met (🟢 READY TO LAUNCH).

### 2. Launch the agent
```bash
.agents/launch.sh P1-V01
```
This does four things automatically:
- Verifies all dependency tasks are `done` in manifest.yaml
- Creates branch `feat/P1-V01` from latest `main`
- Combines the role config + task prompt into `.claude-task-context.md`
- Updates manifest status to `in_progress`

### 3. Start the AI tool
For Claude Code tasks:
```bash
claude
```
Then tell it:
> "Read .claude-task-context.md and execute the task described in it."

For Codex CLI tasks:
```bash
codex "$(cat .claude-task-context.md)"
```

### 4. Watch and guide — DO NOT walk away
The agent will:
- Read the context and start working
- Create files, write code, run commands
- **Ask you questions** when it's uncertain
- **Hit errors** that need your input (missing dependency, unclear spec)
- **Produce intermediate output** you should glance at

Your job during execution:
- Answer questions quickly and specifically
- If it's going in the wrong direction, interrupt: "Stop. The spec says X, not Y."
- If it finishes a file, glance at it — catch major issues early

### 5. Run the quality review
When the agent says it's done:
```bash
.agents/review.sh P1-V01
```
This runs automated checks. See "Quality Review System" below.

### 6. Do the human review
Read the review.sh output. Check the items it CAN'T automate (see per-role
checklists in `.agents/review-checklists/`).

### 7. If quality is not sufficient — give specific feedback
See "How to Fix Mediocre Output" below.

### 8. When satisfied — merge
```bash
git checkout main
git merge feat/P1-V01 --no-ff -m "Merge P1-V01: Edge Agent [DEV]"
git push origin main

# Update manifest
python3 -c "
import yaml
with open('.agents/manifest.yaml') as f:
    m = yaml.safe_load(f)
for phase in m['phases'].values():
    for task in phase['tasks']:
        if task['id'] == 'P1-V01':
            task['status'] = 'done'
with open('.agents/manifest.yaml', 'w') as f:
    yaml.dump(m, f, default_flow_style=False, sort_keys=False)
"
git add .agents/manifest.yaml
git commit -m "Mark P1-V01 done"
git push origin main
```

### 9. Check what's now unblocked
```bash
.agents/status.sh
```
Tasks that depended on P1-V01 may now show as 🟢 READY.

---

## Running Multiple Agents in Parallel

Open multiple terminals (or use tmux):

```bash
# Terminal 1
.agents/launch.sh P1-V01
claude  # Dev agent building edge agent

# Terminal 2
.agents/launch.sh P1-V02
claude  # Dev agent building ingress bridge

# Terminal 3
.agents/launch.sh P1-O01
codex "$(cat .claude-task-context.md)"  # Ops agent building infra
```

**All three agents work on the SAME REPO but on DIFFERENT BRANCHES.**
They don't see each other's work until you merge to main.

Switch between terminals to monitor progress. The agents don't need
constant attention — check in every 5-10 minutes.

---

## Git Branch Model

```
main ← the only branch agents read FROM (via launch.sh)
 ├── feat/P0-D01  ← Design agent working on taxonomy
 ├── feat/P0-O01  ← Ops agent working on infra scaffold
 ├── feat/P0-V01  ← Dev agent working on prototype
 └── feat/P1-V01  ← Dev agent working on edge agent (after P0 deps merged)
```

Every agent commits to its own `feat/{task-id}` branch.
The human merges to `main` after review.
The next agent's `launch.sh` creates its branch from the LATEST `main`.

---

## How to Fix Mediocre Output

### What NOT to do
❌ "This isn't good enough, please improve it."
❌ "Make it more robust."
❌ "Add better error handling."

These produce cosmetic changes. The agent doesn't know what you're dissatisfied with.

### What TO do — give concrete, verifiable feedback

**Missing functionality:**
> "The rtsp_client.py doesn't handle the case where the camera returns H.265
> but the GStreamer pipeline is configured for H.264. Add codec auto-detection
> using the GStreamer caps negotiation, and add a test case with an H.265 fixture."

**Incorrect logic:**
> "In writer.py line 47, you're using `conn.execute(INSERT...)` but the spec
> requires COPY protocol. Replace with `conn.copy_records_to_table('detections',
> records=batch)`. This is a hard requirement for throughput."

**Missing tests:**
> "test_rtsp_client.py only tests successful connection. Add these test cases:
> 1. Camera returns 401 → agent logs error, doesn't retry
> 2. Camera times out → exponential backoff 1s, 2s, 4s, 8s
> 3. Five consecutive decode errors → pipeline restarts
> 4. WAN goes down → local buffer fills → WAN returns → replay drains"

**Spec mismatch:**
> "The Kafka producer key is `camera_id` but docs/kafka-contract.md specifies
> the key must be `{site_id}:{camera_id}:{capture_ts}:{frame_seq}`. Fix the
> key format in publisher.py to match the contract exactly."

### The feedback loop
1. Run `review.sh` — get automated score
2. Read the FAIL items — fix those first (paste each one to the agent)
3. Read the WARN items — fix the important ones
4. Re-run `review.sh` — score should improve
5. If score reaches PASS threshold — do human review
6. Human review catches what automation can't (logic correctness, spec conformance)
