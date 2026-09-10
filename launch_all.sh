#!/bin/zsh
# ONE SHOT, run only on James's explicit "launch":
#   1) deploy RICHY from the dev wallet + 0.11 ETH dev buy
#   2) start the fee keeper (80% of ETH fees -> Richy's trading wallet)
#   3) start Richy trading live
#   4) make sure the feed server + Cloudflare tunnel keeper are up
cd ~/hoodfly
set -a; . ./.env; set +a
export PATH="$HOME/.foundry/bin:$PATH"
echo "$(date) LAUNCH ALL" >> runs/launch_all.log
.venv/bin/python richy_launch.py --name "Richy The Fly" --symbol RICHY --start-fdv-eth 2 --other "$RICHY_OTHER_ADDRESS" --dev-buy-eth 0.11 --live 2>&1 | grep -vE "DeployHelper|Warning" | tee -a runs/launch_all.log | grep -E "coin:|treasury:|launcher|dev buy|addresses|failed|Error"
set -a; . ./.env; set +a   # picks up RICHY_COIN / RICHY_TREASURY written by the launch
if [ -z "$RICHY_TREASURY" ]; then echo "launch failed: no treasury address; NOT starting richy" | tee -a runs/launch_all.log; exit 1; fi
pkill -f richy_fee_keeper.py 2>/dev/null
nohup caffeinate -i .venv/bin/python richy_fee_keeper.py >> runs/richy_keeper.out 2>&1 &
echo "$(date) fee keeper started" >> runs/launch_all.log
pgrep -f "feed_server.py --out runs/live" >/dev/null || (nohup .venv/bin/python feed_server.py --out runs/live --port 8787 >> runs/live/feed.log 2>&1 &)
pgrep -f tunnel_keeper.sh >/dev/null || (nohup caffeinate -i ./tunnel_keeper.sh >/dev/null 2>&1 &)
rm -f runs/live/STOP
grep -q '^STONKFLY_LIVE=' .env || echo 'STONKFLY_LIVE=I_ACCEPT_REAL_TRADES' >> .env
pkill -f "stonkfly.cli run" 2>/dev/null; sleep 1
nohup caffeinate -i .venv/bin/python -m stonkfly.cli run --venue robinhood --live --migrate-source --capital-eth 2 --free-reign --universe 200 --out runs/live >> runs/live/fly.log 2>&1 &
echo "$(date) richy started" >> runs/launch_all.log
sleep 20; grep -v Warning runs/live/fly.log | tail -3
