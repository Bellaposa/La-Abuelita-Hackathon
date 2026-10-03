#!/bin/sh
# Snapshot of the agents' learned state + logs into relevo/, committed and pushed (the repo is private).
# Usage: ./relevo.sh      (run it right before switching machines)
set -e
cd "$(dirname "$0")"
mkdir -p relevo/logs relevo/dashboard
cp memory.json duels_memory.json broker_memory.json bench_model.json bench_snapshots.jsonl historial_abuela.txt relevo/ 2>/dev/null || true
cp dashboard/history.json relevo/dashboard/ 2>/dev/null || true
for f in smart_agent.log smart_duels.log page_hunter.log broker.log; do [ -f "$f" ] && cp "$f" relevo/logs/; done
[ -f dashboard/dashboard.log ] && cp dashboard/dashboard.log relevo/logs/dashboard.log
cp HANDOVER.md relevo/HANDOVER.md
date "+%Y-%m-%d %H:%M:%S" > relevo/SNAPSHOT_TIME
git add -f relevo
git commit -q -m "relevo: snapshot of the agents' state and logs ($(cat relevo/SNAPSHOT_TIME))" || echo "nothing new"
git pull --no-rebase --no-edit -q origin main && git push -q origin main && echo "relevo pushed: $(cat relevo/SNAPSHOT_TIME)"
