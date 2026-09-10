#!/usr/bin/env python3
"""Harvests Richy's LP fees into the fly's trading wallet (80%) and the other wallet (20%).

Every INTERVAL seconds: simulate treasury.harvest(); if the fly's share would be at least
MIN_FLY_ETH, send it from the fly wallet (anyone may call harvest; the fly pays the gas).
The fly's ledger books the incoming ETH as fee income (see broker_rh.verify_balances).
    python richy_fee_keeper.py            # needs RICHY_TREASURY + HOODFLY_PK in .env
"""
import json, os, sys, time
from pathlib import Path

from stonkfly.pons import Rpc, Signer, _u

HERE = Path(__file__).resolve().parent
for ln in (HERE / ".env").read_text().splitlines():
    if ln.strip() and not ln.startswith("#") and "=" in ln:
        k, v = ln.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
TREASURY = os.environ.get("RICHY_TREASURY")
INTERVAL = int(os.environ.get("RICHY_HARVEST_SECS", "600"))
MIN_FLY_ETH = float(os.environ.get("RICHY_MIN_FLY_ETH", "0.0005"))
HARVEST = "0x4641257d"  # harvest()
HARVESTED = "0x" + __import__("web3").Web3.keccak(text="Harvested(uint256,uint256,uint256)").hex().replace("0x", "")

if not TREASURY:
    sys.exit("RICHY_TREASURY not set (launch first)")
rpc = Rpc(); signer = Signer(rpc)
log = (HERE / "runs" / "richy_keeper.log").open("a")
def say(m):
    print(m, flush=True); log.write(f"{time.strftime('%F %T')} {m}\n"); log.flush()
say(f"richy fee keeper: treasury {TREASURY} from {signer.address}, every {INTERVAL}s, min {MIN_FLY_ETH} ETH")
while True:
    try:
        sim = rpc.call("eth_simulateV1", [{"blockStateCalls": [{"calls": [{"from": signer.address, "to": TREASURY, "data": HARVEST}]}], "validation": False}, "latest"])
        c = sim[0]["calls"][0]
        fly_eth = 0.0
        if c.get("status") == "0x1":
            for l in c.get("logs", []):
                if l["topics"][0] == HARVESTED:
                    fly_eth = _u(l["data"], 0) / 1e18
        if fly_eth >= MIN_FLY_ETH:
            txh = signer.send(TREASURY, HARVEST)
            r = signer.receipt(txh, 120)
            say(f"harvest sent {txh} fly_eth={fly_eth:.6f} status={r and r['status']}")
        else:
            say(f"pending fly share {fly_eth:.6f} ETH < min, waiting")
    except Exception as e:
        say(f"error {type(e).__name__}: {str(e)[:160]}")
    time.sleep(INTERVAL)
