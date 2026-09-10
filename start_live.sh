#!/bin/zsh
# Waits for the fly wallet to be funded on Robinhood Chain, then starts the fly (live) + the feed.
cd ~/hoodfly
set -a; . ./.env; set +a
A=${HOODFLY_ADDRESS}
mkdir -p runs/live
echo "$(date) waiting for ETH at $A" >> runs/live/start.log
while true; do
  bal=$(curl -s -m 10 -X POST "$RH_RPC_URL" -H 'content-type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getBalance\",\"params\":[\"$A\",\"latest\"]}" \
    | python3 -c "import sys,json;print(int(json.load(sys.stdin).get('result','0x0'),16))" 2>/dev/null)
  if [ -n "$bal" ] && [ "$bal" -gt 1000000000000000 ]; then break; fi   # > 0.001 ETH
  sleep 20
done
echo "$(date) funded: $bal wei" >> runs/live/start.log
.venv/bin/python -m stonkfly.cli run --venue robinhood --live --preflight-only --out runs/live >> runs/live/start.log 2>&1 || { echo "$(date) PREFLIGHT FAILED" >> runs/live/start.log; exit 1; }
pkill -f "feed_server.py --out runs/live" 2>/dev/null
nohup .venv/bin/python feed_server.py --out runs/live --port 8787 >> runs/live/feed.log 2>&1 &
echo "$(date) fly starting" >> runs/live/start.log
exec .venv/bin/python -m stonkfly.cli run --venue robinhood --live --migrate-source --capital-eth 2 --free-reign --universe 200 --out runs/live >> runs/live/fly.log 2>&1
