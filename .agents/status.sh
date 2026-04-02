#!/usr/bin/env bash
python3 << 'PYEOF'
import yaml

with open(".agents/manifest.yaml") as f:
    m = yaml.safe_load(f)

status_map = {}
all_tasks = []
for phase_key, phase in m["phases"].items():
    for task in phase["tasks"]:
        status_map[task["id"]] = task["status"]
        all_tasks.append((phase_key, task))

counts = {"pending": 0, "in_progress": 0, "in_review": 0, "done": 0}
for _, t in all_tasks:
    counts[t["status"]] = counts.get(t["status"], 0) + 1

print("=" * 60)
print(f'  📊 Tasks: {counts["done"]} done | {counts["in_progress"]} active | {counts["pending"]} pending')
print("=" * 60)

print("\n🟢 READY TO LAUNCH:")
for _, task in all_tasks:
    if task["status"] != "pending":
        continue
    deps = task.get("depends_on") or []
    blocked = [d for d in deps if status_map.get(d) != "done"]
    if not blocked:
        print(f'   {task["id"]:8s} [{task["role"]:6s}] {task["title"]:45s} → {task["tool"]}')

print("\n🔵 IN PROGRESS:")
for _, task in all_tasks:
    if task["status"] == "in_progress":
        print(f'   {task["id"]:8s} [{task["role"]:6s}] {task["title"]:45s} branch: {task.get("branch","?")}')

print("\n🟡 BLOCKED:")
for _, task in all_tasks:
    if task["status"] != "pending":
        continue
    deps = task.get("depends_on") or []
    blocked = [d for d in deps if status_map.get(d) != "done"]
    if blocked:
        print(f'   {task["id"]:8s} waiting on: {blocked}')
print()
PYEOF
