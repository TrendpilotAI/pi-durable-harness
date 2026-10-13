#!/usr/bin/env bash
# Start a real session against local mlx-serve, SIGKILL it mid-run, show status, then resume by id.
set -euo pipefail
cd "$(dirname "$0")/.."
export PDH_HOME="${PDH_HOME:-$PWD/runs/demo-home}"
ID="${1:-kill-demo-$(date +%H%M%S)}"
WS="$PWD/runs/demo-ws/$ID"
KILL_AFTER="${KILL_AFTER:-4}"
LOG="$PWD/runs/demo-logs/$ID"; mkdir -p "$LOG"
PROMPT='Use the agent tool for each step, one at a time: (1) agent_a writes fib.py with fib(n) iteratively; (2) agent_b writes test_fib.py with unittest covering n=0,1,2,10,30 and runs python3 -m unittest -q; (3) agent_c writes README.md describing both files. After each step, verify with bash (ls, run tests). Finish with a summary of every step.'
node src/cli.ts start --team --id "$ID" --cwd "$WS" "$PROMPT" </dev/null >"$LOG/start.out" 2>"$LOG/start.err" &
PID=$!
echo "started pid=$PID id=$ID; will SIGKILL ${KILL_AFTER}s after fib.py appears (mid-run, between/inside sub-agent calls)"
for _ in $(seq 1 240); do [[ -f "$WS/fib.py" ]] && break; sleep 1; done
sleep "$KILL_AFTER"
# Kill the real node process that owns the store (Volta's node shim is a parent process).
OWNER=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('pid',''))" "$PDH_HOME/sessions/$ID/meta.json")
echo "SIGKILL owner pid=$OWNER (shim pid=$PID)"
kill -9 "$OWNER" || true
wait "$PID" 2>/dev/null || true
echo "--- after SIGKILL: list"; node src/cli.ts list | tee "$LOG/list-after-kill.txt"
echo "--- after SIGKILL: status"; node src/cli.ts status "$ID" | tee "$LOG/status-after-kill.json" | head -60
echo "--- files in workspace at kill time"; ls -la "$WS" || true | tee "$LOG/ws-after-kill.txt"
echo "--- resume"; node src/cli.ts resume "$ID" </dev/null 2>"$LOG/resume.err" | tee "$LOG/resume.out"
echo "--- transcript after resume (newest first)"; node src/cli.ts transcript "$ID" | tee "$LOG/transcript.jsonl" | cut -c1-300
echo "--- status after resume"; node src/cli.ts status "$ID" | tee "$LOG/status-after-resume.json" | head -40
echo "--- workspace after resume"; ls -la "$WS"; (cd "$WS" && python3 -m unittest -q 2>&1 | tail -3) | tee "$LOG/tests-after-resume.txt"
