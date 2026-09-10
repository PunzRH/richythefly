#!/usr/bin/env python3
"""Richy's coin, dev'd by the fly's own wallet (HOODFLY_PK) on Robinhood Chain.

ETH-quoted Uniswap V4 pool, 3% pool fee on buys and sells, single-sided LOCKED liquidity
(locked single-sided V4 liquidity). Fees go to RichyTreasury: FLY_BPS (default 80%) of the ETH to the
fly's trading wallet, the rest to --other, coin-side fees burned. Anyone can call harvest().

    python richy_launch.py --name "Richy" --symbol RICHY --start-fdv-eth 2 --other 0xYourWallet          # dry run (simulates)
    python richy_launch.py --name "Richy" --symbol RICHY --start-fdv-eth 2 --other 0xYourWallet --live   # deploys

Needs ~0.01 ETH of gas in the fly wallet. Writes runs/richy.json and RICHY_* lines to .env.
"""
import argparse, json, math, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PERP = Path.home() / "pons-perp"
FORGE = str(Path.home() / ".foundry/bin/forge")


def load_env():
    for ln in (HERE / ".env").read_text().splitlines():
        if ln.strip() and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def start_tick(supply, fdv_eth, span=69_000):
    """V4Launch wants the tick of price = coin units per 1 ETH at the START of the curve."""
    coin_per_eth = supply / fdv_eth
    t = int(math.log(coin_per_eth) / math.log(1.0001))
    return t - (t % 200)


def dev_buy(info, eth, rpc_url, pk):
    """Dev buy from the DEV wallet through the same V4 router the site uses; estimate first, then send."""
    sys.path.insert(0, str(HERE))
    from stonkfly.pons import Rpc, Signer, _pad
    from stonkfly.pons_v4 import ETH, POOL_MANAGER, ROUTER
    rpc = Rpc(); signer = Signer(rpc, pk)
    w = lambda v: hex(int(v))[2:].rjust(64, "0")
    amount = int(eth * 10**18)
    words = [w(0xA0), w(0), w(amount), w(0), w(int(time.time()) + 600), w(1), w(0x20), w(2),
             _pad(ETH), _pad(info["coin"]), w(0), w(30_000), w(200), _pad("0x" + "0" * 40), w(0x140), _pad(POOL_MANAGER), w(0), w(0)]
    data = "0x4d819a2a" + "".join(words)
    gas = signer.estimate(ROUTER, data, amount)  # reverts here cost nothing
    txh = signer.send(ROUTER, data, amount, gas)
    r = signer.receipt(txh, 120)
    print(f"dev buy {eth} ETH tx {txh} status {r and r['status']}")
    info["dev_buy_eth"] = eth; info["dev_buy_tx"] = txh
    (HERE / "runs" / "richy.json").write_text(json.dumps(info, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--start-fdv-eth", type=float, default=2.0, help="starting fully-diluted value in ETH")
    ap.add_argument("--supply", type=float, default=1_000_000_000)
    ap.add_argument("--fee-pct", type=float, default=3.0, help="pool fee %% on every buy and sell")
    ap.add_argument("--fly-pct", type=float, default=80.0, help="%% of ETH fees to the fly's trading wallet")
    ap.add_argument("--other", required=True, help="wallet for the remaining %% of fees")
    ap.add_argument("--span", type=int, default=69_000)
    ap.add_argument("--token-uri", default=json.load(open(HERE / "runs" / "richy_meta.json"))["token_uri"] if (HERE / "runs" / "richy_meta.json").exists() else "")
    ap.add_argument("--website", default="https://richythefly.com")
    ap.add_argument("--twitter", default="https://x.com/RichyTheFly")
    ap.add_argument("--dev-buy-eth", type=float, default=0.0, help="dev buy from the DEV wallet right after launch (0.11 ETH = 5%% at a 2 ETH start)")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    load_env()
    pk, rpc = os.environ.get("RICHY_DEV_PK"), os.environ.get("RH_RPC_URL")
    fly = os.environ.get("HOODFLY_ADDRESS")
    if not pk or not rpc or not fly:
        sys.exit("RICHY_DEV_PK / HOODFLY_ADDRESS / RH_RPC_URL missing in .env")
    tick = start_tick(a.supply, a.start_fdv_eth, a.span)
    env = {**os.environ, "NAME": a.name, "SYMBOL": a.symbol, "SUPPLY": str(int(a.supply * 10**18)),
           "START_TICK": str(tick), "SPAN": str(a.span), "FEE": str(int(a.fee_pct * 10_000)),
           "FLY_BPS": str(int(a.fly_pct * 100)), "OTHER": a.other, "RH_RPC_URL": rpc,
           "TOKEN_URI": a.token_uri, "WEBSITE": a.website, "TWITTER": a.twitter, "FLY": fly}
    cmd = [FORGE, "script", "script/LaunchRichy.s.sol", "--rpc-url", rpc, "--private-key", pk]
    if a.live:
        cmd.append("--broadcast")
    print(f"{'LIVE LAUNCH' if a.live else 'DRY RUN'}: {a.name} ({a.symbol}) supply {a.supply:,.0f} start FDV {a.start_fdv_eth} ETH "
          f"(tick {tick}) fee {a.fee_pct}% fly {a.fly_pct}% other {a.other}", flush=True)
    r = subprocess.run(cmd, cwd=PERP, env=env, capture_output=True, text=True)
    out = r.stdout + r.stderr
    print(out[-3000:])
    if r.returncode != 0:
        sys.exit("forge failed")
    addrs = {k: v for k, v in re.findall(r"(coin|treasury|launcher \(locked LP\)): (0x[a-fA-F0-9]{40})", out)}
    if a.live and addrs:
        info = {"name": a.name, "symbol": a.symbol, "coin": addrs.get("coin"), "treasury": addrs.get("treasury"),
                "launcher": addrs.get("launcher (locked LP)"), "fee_pct": a.fee_pct, "fly_pct": a.fly_pct, "other": a.other,
                "start_fdv_eth": a.start_fdv_eth, "start_tick": tick, "dev_wallet": os.environ.get("RICHY_DEV_ADDRESS"), "fly_wallet": fly, "token_uri": a.token_uri, "website": a.website, "twitter": a.twitter, "launched_at": time.time()}
        (HERE / "runs" / "richy.json").write_text(json.dumps(info, indent=2))
        with open(HERE / ".env", "a") as f:
            f.write(f"RICHY_COIN={info['coin']}\nRICHY_TREASURY={info['treasury']}\nRICHY_LAUNCHER={info['launcher']}\n")
        print("saved runs/richy.json and RICHY_* in .env")
        if a.dev_buy_eth > 0:
            dev_buy(info, a.dev_buy_eth, rpc, pk)
    print("addresses:", addrs)


if __name__ == "__main__":
    main()
