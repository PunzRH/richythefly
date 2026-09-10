#!/usr/bin/env python3
"""Hoodfly live feed: turns a running `stonkfly run --venue robinhood` into JSON a website polls.

Reads the run dir (ledger.sqlite read-only + events.jsonl) and serves:
    GET /feed.json   everything the UI needs (state, fly, universe, trades, learning)
    GET /events      Server-Sent Events stream of the same object every 2 s (for live animation)

    python feed_server.py --out runs/live --port 8787

All money is ETH on Robinhood Chain. Fresh, unfiltered: whatever the fly did last tick.
"""
import argparse, json, os, sqlite3, time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _sym(product):  # "DUROV-0x703d..." -> ("DUROV", "0x703d...")
    s, _, a = str(product).partition("-")
    return s.upper(), a


def _tail(path, n=400):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 2_000_000))
        for line in f.read().decode(errors="ignore").splitlines()[-n:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _richy(out):
    """Richy coin addresses (from runs/richy.json) for the site."""
    p = os.path.join(out, "richy.json")
    if not os.path.exists(p):
        p = os.path.join(os.path.dirname(out.rstrip("/")) or ".", "richy.json")
    try:
        return json.load(open(p))
    except Exception:
        return None


def build_feed(out):
    db_path = os.path.join(out, "ledger.sqlite")
    if not os.path.exists(db_path):
        return {"status": "waiting", "ts": time.time()}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        meta = {}
        for k, v in db.execute("SELECT key,value FROM meta"):
            try:
                meta[k] = json.loads(v)
            except Exception:
                meta[k] = v
    finally:
        db.close()
    cash = Decimal(str(meta.get("cash", "0")))
    initial = Decimal(str(meta.get("initial_cash", "0")))
    positions = {k: Decimal(str(v)) for k, v in (meta.get("positions") or {}).items() if Decimal(str(v)) > 0}
    obs = meta.get("observation") or {}
    basis = meta.get("basis") or {}
    smell = obs.get("smell") or {}
    events = _tail(os.path.join(out, "events.jsonl"))

    # last known executable bid per product (from the latest tick's quotes in the history)
    hist = obs.get("market_history") or {}
    last_mid = {p: (h[-1] if h else 0.0) for p, h in hist.items()}
    q = obs.get("quote") or {}
    if q.get("product"):
        last_mid[q["product"]] = float(q.get("bid") or last_mid.get(q["product"], 0))

    pos_rows, staked = [], Decimal("0")
    for p, tokens in positions.items():
        sym, addr = _sym(p)
        px = Decimal(str(last_mid.get(p, 0)))
        value = tokens * px
        staked += value
        cost = Decimal(str((basis.get(p) or {}).get("eth", "0")))
        pos_rows.append({
            "symbol": sym, "address": addr, "tokens": float(tokens), "value_eth": float(value),
            "cost_eth": float(cost), "unrealized_eth": float(value - cost), "smell": (basis.get(p) or {}).get("key"),
            "graduated": bool((smell.get(p) or {}).get("graduated")),
        })
    equity = cash + staked

    trades, wins, losses, realized_total = [], 0, 0, 0.0
    curve = []
    for r in events:
        ex = r.get("execution") or {}
        curve.append([r.get("wall_time"), float(r.get("equity_usdc", 0))])
        if ex.get("status") in ("FILLED", "SETTLED"):
            sym, addr = _sym(r["product"])
            base = float(ex.get("base") or 0)
            quote = float(ex.get("quote") or 0)
            pnl = ex.get("realized_pnl_eth")
            pnl = None if pnl is None else float(pnl)
            if pnl is not None:
                realized_total += pnl
                wins += pnl > 0
                losses += pnl <= 0
            trades.append({
                "t": r.get("wall_time"), "tick": r.get("tick"), "action": (r.get("neural") or {}).get("side"),
                "symbol": ex.get("symbol") or sym, "name": ex.get("name"), "address": addr,
                "tokens": base, "eth": quote, "fee_eth": float(ex.get("fee") or 0),
                "price_eth": (quote / base) if base else None, "realized_pnl_eth": pnl,
                "tx": ex.get("tx"), "smell": ex.get("smell"), "mode": r.get("mode"),
            })
    trades = trades[-80:][::-1]

    neural = obs.get("neural") or {}
    side = neural.get("side", "HOLD")
    diff = float(neural.get("difference_hz") or 0)
    drive = max(0.05, min(0.95, 0.5 + diff / 40.0))
    delta = float(Decimal(str(obs.get("pnl_delta_usdc", "0"))))
    coin = obs.get("coin") or {}
    universe = []
    for p, h in smell.items():
        sym, addr = _sym(p)
        universe.append({"symbol": h.get("symbol") or sym, "address": addr, **{k: h.get(k) for k in ("real_eth", "flow_eth_min", "progress", "age_s", "held", "graduated", "buyers", "buys", "top_buyer_share", "bundled_buys", "fake_score", "trades_5m", "eth_5m", "dev_share", "rugged", "peak_eth", "ramp_score")},
                         "looking": p == obs.get("product")})
    universe.sort(key=lambda u: (u.get("flow_eth_min") or 0), reverse=True)

    taste = meta.get("taste") or {}
    taste_rows = sorted(
        [{"smell": k, "trades": v["n"], "wins": v["wins"], "avg_return": round(v["sum"] / max(v["n"], 1), 4)} for k, v in taste.items()],
        key=lambda x: x["avg_return"], reverse=True,
    )
    last_event = events[-1] if events else {}
    stopped = os.path.exists(os.path.join(out, "STOP")) or (last_event.get("wall_time") and time.time() - float(last_event["wall_time"]) > 240)
    return {
        "status": "halted" if meta.get("halted") else ("stopped" if stopped else "live"),
        "mode": meta.get("mode"),
        "wallet": meta.get("wallet"),
        "chain": "robinhood-chain",
        "tick": meta.get("tick"),
        "halted": meta.get("halted"),
        "balance_eth": float(equity),
        "cash_eth": float(cash),
        "staked_eth": float(staked),
        "initial_eth": float(initial),
        "pnl_eth": float(equity - initial),
        "pnl_pct": float((equity - initial) / initial * 100) if initial else 0.0,
        "fee_income_eth": float(Decimal(str(meta.get("fee_income_eth") or "0"))),
        "richy": _richy(out),
        "fly": {
            "decision": side,
            "coin": coin.get("symbol"),
            "name": coin.get("name"),
            "address": coin.get("coin"),
            "curve": coin.get("curve"),
            "smell": obs.get("smell_key"),
            "drive": round(drive, 3),
            "left_hz": neural.get("left_hz"),
            "right_hz": neural.get("right_hz"),
            "gate_spikes": neural.get("gate_spikes"),
            "reward_spikes": neural.get("reward_spikes"),
            "aversive_spikes": neural.get("aversive_spikes"),
            "kc_spikes": neural.get("KC_spikes"),
            "total_spikes": neural.get("total_spikes"),
            "stimulus": neural.get("stimulus"),
            "dopamine": {"reward": delta > 0, "aversive": delta < 0, "delta_eth": delta},
            "plastic_edges_changed": (neural.get("memory") or {}).get("changed_edges"),
            "last_execution": (last_event.get("execution") or {}).get("status"),
            "last_execution_reason": (last_event.get("execution") or {}).get("reason"),
            "last_tick_at": last_event.get("wall_time"),
            "price_history": [float(x) for x in (hist.get(obs.get("product")) or [])][-120:],
            "smell_key": obs.get("smell_key"),
            "nose": smell.get(obs.get("product")),
        },
        "universe": universe,
        "positions": pos_rows,
        "trades": trades,
        "learning": {
            "closed_trades": wins + losses, "wins": wins, "losses": losses,
            "win_rate": (wins / (wins + losses)) if (wins + losses) else None,
            "realized_total_eth": realized_total,
            "taste": taste_rows,
            "equity_curve": curve[-300:],
        },
        "ts": time.time(),
    }


class Handler(BaseHTTPRequestHandler):
    out = "runs/paper"
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/feed.json", "/", "/feed"):
            try:
                body = json.dumps(build_feed(self.out)).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors(); self.end_headers(); self.wfile.write(body)
            except Exception as e:
                err = json.dumps({"error": str(e)}).encode()
                self.send_response(500); self.send_header("Content-Length", str(len(err))); self._cors(); self.end_headers()
                self.wfile.write(err)
            return
        if path in ("/spikes.json", "/neurons.json"):
            # spikes.json = this tick's real firing (written by the run loop);
            # neurons.json = static soma positions + circuit groups (runs/neurons.json)
            f = os.path.join(self.out, "spikes.json") if path == "/spikes.json" else os.path.join(os.path.dirname(self.out.rstrip("/")) or ".", "neurons.json")
            if not os.path.exists(f):
                self.send_response(404); self._cors(); self.end_headers(); return
            body = open(f, "rb").read()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors(); self.end_headers(); self.wfile.write(body); return
        if path == "/events":
            self.send_response(200); self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close"); self.send_header("X-Accel-Buffering", "no")
            self._cors(); self.end_headers()
            try:
                while True:
                    self.wfile.write(b"data: " + json.dumps(build_feed(self.out)).encode() + b"\n\n")
                    self.wfile.flush(); time.sleep(2)
            except (BrokenPipeError, ConnectionResetError):
                return
        self.send_response(404); self._cors(); self.end_headers()

    def log_message(self, *a):
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="runs/paper", help="the run dir (ledger.sqlite + events.jsonl)")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    Handler.out = a.out
    print(f"hoodfly feed -> http://localhost:{a.port}/feed.json  and  /events (SSE)   reading {a.out}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
