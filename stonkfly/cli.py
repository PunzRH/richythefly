"""Single-worker run loop. Default execution is paper; live must be explicit."""

import argparse
import dataclasses
import fcntl
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from .config import D, Settings


def main():
    p = argparse.ArgumentParser(prog="stonkfly")
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--reuse-doomfly", type=Path)
    sub.add_parser("verify")
    run = sub.add_parser("run")
    run.add_argument("--live", action="store_true")
    run.add_argument(
        "--venue",
        default="coinbase",
        choices=["coinbase", "robinhood"],
        help="coinbase (BTC/ETH/SOL) or robinhood (the fly forages Pons V2 coins on Robinhood Chain by itself)",
    )
    run.add_argument("--order-eth", default="0.002", help="robinhood: ETH per order")
    run.add_argument("--loss-stop-eth", default="0.02", help="robinhood: halt when equity drops this much below start")
    run.add_argument("--capital-eth", default="0.1", help="robinhood: max ETH the fly may be given (paper: starting ETH)")
    run.add_argument("--interval", type=float, default=None, help="seconds between decisions (robinhood min 15, coinbase min 60)")
    run.add_argument("--universe", type=int, default=20, help="robinhood: how many live coins the fly looks at per tick")
    run.add_argument("--max-coin-pct", type=float, default=15.0, help="robinhood: max %% of starting capital in any one coin")
    run.add_argument("--max-invested-pct", type=float, default=60.0, help="robinhood: max %% of starting capital in coins at once")
    run.add_argument("--free-reign", action="store_true", help="robinhood: no fixed order size or cadence; the fly sizes orders by neural conviction, ticks as fast as it thinks")
    run.add_argument("--migrate-source", action="store_true", help="accept that the source changed since this run started (records old signature in provenance_history)")
    run.add_argument(
        "--preflight-only",
        action="store_true",
        help="Read-only exchange checks; never submit an order",
    )
    run.add_argument(
        "--resume-reviewed",
        action="store_true",
        help="After manual review, clear a transient halt only after successful reconciliation",
    )
    run.add_argument(
        "--fixture",
        action="store_true",
        help="Synthetic offline market input; paper only",
    )
    run.add_argument("--steps", type=int, default=0, help="0 keeps running")
    run.add_argument(
        "--fast",
        action="store_true",
        help="Skip waiting in paper mode; execution cooldown still applies",
    )
    run.add_argument(
        "--frozen",
        action="store_true",
        help="Freeze all memory efficacies for a control run",
    )
    run.add_argument("--out", type=Path)
    run.add_argument(
        "--products",
        nargs="+",
        default=["BTC-USDC"],
        choices=["BTC-USDC", "ETH-USDC", "SOL-USDC"],
    )
    run.add_argument("--neural-ms", type=float, default=500)
    status = sub.add_parser("status")
    status.add_argument("--out", type=Path, default=Path("runs/paper"))
    a = p.parse_args()
    from dotenv import load_dotenv

    # Never search parent projects for unrelated account credentials.
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    if a.command in ("prepare", "verify"):
        from .data import prepare, verify

        if a.command == "prepare":
            prepare(a.reuse_doomfly)
        else:
            print(json.dumps(verify()))
        return
    if a.command == "status":
        import sqlite3

        db = sqlite3.connect(f"file:{a.out / 'ledger.sqlite'}?mode=ro", uri=True)
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
        print(
            json.dumps(
                {
                    k: meta.get(k)
                    for k in [
                        "mode",
                        "tick",
                        "cash",
                        "positions",
                        "initial_cash",
                        "anchor",
                        "halted",
                    ]
                },
                indent=2,
            )
        )
        return
    if a.live and (a.fixture or a.fast):
        p.error("Live mode forbids fixtures and fast replay")
    if a.steps < 0:
        p.error("steps cannot be negative")
    if a.venue == "robinhood":
        # Everything is denominated in ETH on Robinhood Chain. The product list is
        # dynamic (whatever is live on the Pons curves); the placeholder only seeds Settings.
        order = D(a.order_eth)
        free = a.free_reign
        settings = Settings(
            products=("PONS-ANY",),
            venue="robinhood",
            capital=a.capital_eth,
            order_limit=a.capital_eth if free else a.order_eth,   # free reign: the fly sizes every order itself
            loss_stop=str(D(a.capital_eth) * D("0.9")) if free else a.loss_stop_eth,  # only a floor: halt if 90% is gone
            fee_reserve="0.05",
            slippage="0.2" if free else "0.08",
            spread_limit="0.3" if free else "0.25",
            daily_orders=50000 if free else 600,
            interval_seconds=a.interval or (5 if free else 30),
            reward_deadband=str((D(a.capital_eth) if free else order) * D("0.002")),
            max_quote_age=40,
            paper_fee="0.02",
            learning=not a.frozen,
            neural_ms=a.neural_ms,
            pulse_ms=min(200, a.neural_ms),
        )
    else:
        settings = Settings(
            products=tuple(a.products),
            venue=a.venue,
            learning=not a.frozen,
            neural_ms=a.neural_ms,
            pulse_ms=min(200, a.neural_ms),
            **({"interval_seconds": a.interval} if a.interval else {}),
        )
    out = a.out or Path("runs/live" if a.live else "runs/paper")
    out.mkdir(parents=True, exist_ok=True)
    lock = (out / "worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A worker already owns this run directory")
    from .broker import CoinbaseBroker, PaperBroker
    from .ledger import Ledger

    ledger = Ledger(out / "ledger.sqlite", settings, "live" if a.live else "paper")
    try:
        market = None
        if a.venue == "robinhood":
            from .market_rh import FixtureRHMarket, RobinhoodChainMarket

            market = (
                FixtureRHMarket(order_eth=a.order_eth)
                if a.fixture
                else RobinhoodChainMarket(order_eth=a.order_eth, universe=a.universe)
            )
            if a.live:
                from .broker_rh import PonsBroker

                broker = PonsBroker.from_env(settings, ledger, market)
            else:
                broker = PaperBroker(settings, ledger)
        else:
            broker = (
                CoinbaseBroker.from_env(settings, ledger)
                if a.live
                else PaperBroker(settings, ledger)
            )
        result = broker.preflight()
        print(json.dumps(result), flush=True)
        if a.resume_reviewed:
            if (out / "STOP").exists() or ledger.pending():
                raise RuntimeError(
                    "Remove STOP only after review; unresolved orders cannot resume"
                )
            reason = ledger.get("halted")
            if reason and ("Loss stop" in reason or "fee exceeded" in reason):
                raise RuntimeError("A financial stop cannot be cleared by this flag")
            ledger.put("halted", None)
        if a.preflight_only:
            return
        from .data import verify

        verified = verify()
        from PIL import Image

        from .actions import StonkflyActions
        from .display import market_frame
        from .market import CoinbaseMarket, FixtureMarket
        from .neural.controller import FlyController
        from .reinforcement import reinforcement
        from .risk import Guard, Veto

        if market is None:
            market = (
                FixtureMarket(settings.products)
                if a.fixture
                else CoinbaseMarket(settings.products)
            )
        from .taste import Taste

        taste = Taste(ledger) if a.venue == "robinhood" else None
        previous = ledger.get("observation")
        if previous:
            market.history = previous["market_history"]
            if a.fixture:
                market.tick = previous["fixture_tick"]
        controller = FlyController(settings)
        cp = ledger.get("checkpoint")
        if cp:
            path = out / cp["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != cp["sha256"]:
                raise RuntimeError("Checkpoint integrity mismatch")
            controller.restore(path)
        provenance = {
            "settings": dataclasses.asdict(settings),
            "dataset": verified,
            "circuit": controller.brain.circuit["report"],
            "vision": controller.brain.visual_report,
            "mode": broker.mode,
            "feed": "fixture" if a.fixture else ("pons-onchain" if a.venue == "robinhood" else "coinbase-public"),
            "decoder": "DNp20 mean R-L: buy/sell; DNpe017 spike gate; otherwise hold. Engineered fixed mapping.",
            "learning_validated": False,
            "pain_receptors_modeled": False,
            "timing": "Each observation advances configured neural_ms regardless of wall-market time; no claim of real-time fly physiology.",
            "source_sha256": {
                str(path.relative_to(Path(__file__).parent)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in Path(__file__).parent.rglob("*")
                if path.suffix in (".py", ".cpp")
            },
        }
        signature = hashlib.sha256(
            json.dumps(provenance, sort_keys=True).encode()
        ).hexdigest()
        if ledger.get("provenance_sha256") not in (None, signature):
            if not a.migrate_source:
                raise RuntimeError(
                    "Run source/protocol changed; use a separate paper run or explicitly review migration"
                )
            hist = ledger.get("provenance_history") or []
            hist.append({"sha256": ledger.get("provenance_sha256"), "replaced_at": time.time()})
            ledger.put("provenance_history", hist)
        ledger.put("provenance_sha256", signature)
        (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        guard = Guard(settings, ledger, out / "STOP")
        provider = StonkflyActions(guard, broker)
        action = provider.get_actions()[0]
        count = 0
        while not a.steps or count < a.steps:
            started = time.monotonic()
            if (out / "STOP").exists() or ledger.get("halted"):
                break
            broker.reconcile()
            broker.verify_balances()
            if taste is not None:
                market.holdings = {p: v for p, v in ledger.positions.items() if v > 0}
            quotes = market.snapshot()
            try:
                guard.check(quotes, time.time())
            except Veto as e:
                if taste is None or ledger.get("halted"):
                    raise
                # dynamic universe: a transient market veto skips this tick instead of killing the run
                print(json.dumps({"tick": ledger.get("tick"), "skipped": str(e)}), flush=True)
                time.sleep(5)
                continue
            market.record(quotes)
            if taste is not None:
                # The fly forages: it picks the coin itself (scent + learned taste + curiosity).
                product = market.choose(ledger.get("tick"), quotes, ledger.positions, taste)
            else:
                product = settings.products[ledger.get("tick") % len(settings.products)]
            q = quotes[product]
            cash_before, held_before = ledger.cash, ledger.positions.get(product, D(0))
            equity = ledger.equity(quotes)
            kind, delta = reinforcement(
                equity, ledger.get("anchor"), settings.reward_deadband
            )
            frame = market_frame(product, market.history[product], q.bid, q.ask)
            neural = controller.observe(frame, kind)
            # Checkpoint + accounting anchor are committed before any trade.
            # Two slots keep the last committed snapshot safe during a crash.
            slot = ledger.get("tick") % 2
            checkpoint = out / f"brain-{slot}.npz"
            controller.save(checkpoint)
            checkpoint_info = {
                "file": checkpoint.name,
                "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            }
            observation = {
                "neural": neural,
                "product": product,
                "quote": q.json(),
                "pnl_delta_usdc": str(delta),
                "market_history": market.history,
                "fixture_tick": getattr(market, "tick", None),
                "smell": getattr(market, "hype", {}),
                "coin": getattr(market, "meta", {}).get(product),
                "smell_key": getattr(market, "features", {}).get(product),
                "universe": list(quotes),
            }
            ledger.commit_tick(equity, checkpoint_info, observation)
            order = {"status": "HOLD"}
            if neural["side"] != "HOLD":
                try:
                    # Neural integration can be slow; use a fresh execution book.
                    fresh = market.snapshot()
                    if product not in fresh:
                        raise Veto("Coin left the fly's view before execution")
                    latest = fresh[product]
                    provider.budget = provider.sell_fraction = None
                    if a.free_reign:
                        # Conviction = how hard the steering neurons lean (DNp20 right vs left).
                        lhz, rhz = float(neural.get("left_hz") or 0), float(neural.get("right_hz") or 0)
                        conviction = min(1.0, max(0.05, abs(rhz - lhz) / max(lhz + rhz, 1.0) * 3.0))
                        if neural["side"] == "BUY":
                            nose = market.hype.get(product, {})
                            if nose.get("rugged") or nose.get("fake_score", 0) >= 0.5:
                                raise Veto(f"Nose veto: smells like a {'rug' if nose.get('rugged') else 'ramp/wash chart'} (fake {nose.get('fake_score')}, ramp {nose.get('ramp_score')})")
                            spend = (ledger.cash - D("0.0005")) * D(str(round(conviction, 4)))
                            spend = min(spend, ledger.cash * D("0.25"))  # guardrail: never more than a quarter of cash in one order
                            cap = market.max_buy_eth(product)
                            if cap is not None:
                                spend = min(spend, cap)
                            held0 = ledger.positions.get(product, D(0))
                            b0 = (taste.basis.get(product) or {})
                            if held0 > 0 and D(b0.get("tokens", "0")) > 0:
                                avg_cost = D(b0.get("eth", "0")) / D(b0.get("tokens", "1"))
                                if avg_cost > 0 and latest.bid < avg_cost * D("0.7"):
                                    raise Veto("Not adding to a position that is down more than 30%")
                            px = market.price_for(product, "BUY", spend) if spend > 0 else None
                            if px:
                                latest = dataclasses.replace(latest, ask=px, bid=min(latest.bid, px))
                            provider.budget = spend
                        else:
                            held = ledger.positions.get(product, D(0))
                            px = market.price_for(product, "SELL", held * D(str(round(conviction, 4)))) if held > 0 else None
                            if px:
                                latest = dataclasses.replace(latest, bid=px, ask=max(latest.ask, px))
                            provider.sell_fraction = conviction
                        fresh = {**fresh, product: latest}
                        neural["conviction"] = round(conviction, 3)
                    if abs(latest.bid - q.bid) / q.bid > D(settings.slippage):
                        raise Veto("Price moved beyond neural observation tolerance")
                    if taste is not None and neural["side"] == "BUY" and not a.free_reign:
                        # Never pile the whole balance into one coin: per-coin and total exposure caps.
                        start = D(ledger.get("initial_cash"))
                        order_eth = D(settings.order_limit)
                        pos = ledger.positions
                        in_coin = pos.get(product, D(0)) * latest.bid
                        invested = sum((pos.get(pp, D(0)) * fresh[pp].bid for pp in pos if pp in fresh), D(0))
                        if in_coin + order_eth > start * D(str(a.max_coin_pct)) / 100:
                            raise Veto(f"Coin exposure cap ({a.max_coin_pct:g}% of capital)")
                        if invested + order_eth > start * D(str(a.max_invested_pct)) / 100:
                            raise Veto(f"Total exposure cap ({a.max_invested_pct:g}% of capital)")
                    provider.quotes = fresh
                    order = action.invoke({"product": product, "side": neural["side"]})
                except Veto as e:
                    order = {"status": "VETO", "reason": str(e)}
            realized = None
            if taste is not None and order.get("status") in ("FILLED", "SETTLED"):
                # Book the fill against cost basis; a sell teaches the taste memory.
                d_tokens = ledger.positions.get(product, D(0)) - held_before
                d_cash = ledger.cash - cash_before
                realized = taste.book(product, market.features.get(product), d_tokens, d_cash, cash_before)
                last = ledger.db.execute(
                    "SELECT settlement FROM orders WHERE status='SETTLED' ORDER BY created DESC LIMIT 1"
                ).fetchone()
                fill = json.loads(last[0]) if last and last[0] else {}
                order = {
                    **order,
                    **{k: fill[k] for k in ("base", "quote", "fee") if k in fill},
                    "realized_pnl_eth": None if realized is None else str(realized),
                    "smell": market.features.get(product),
                    "symbol": market.meta.get(product, {}).get("symbol"),
                    "name": market.meta.get(product, {}).get("name"),
                }
            row = {
                "tick": ledger.get("tick"),
                "wall_time": time.time(),
                "product": product,
                "mode": broker.mode,
                "quote": q.json(),
                "equity_usdc": str(equity),
                "pnl_delta_usdc": str(delta),
                "neural": neural,
                "execution": order,
            }
            with (out / "events.jsonl").open("a") as f:
                f.write(json.dumps(row, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            Image.fromarray(frame).save(out / "latest-input.png")
            (out / "latest.json").write_text(json.dumps(row, indent=2) + "\n")
            # Live neural activity for the site: the real spike counts of this tick,
            # top firing cells (index into runs/neurons.json) + the circuit populations.
            try:
                import numpy as np

                counts = controller.brain.counts
                top = np.argsort(counts)[-600:][::-1]
                top = top[counts[top] > 0]
                circ = controller.brain.circuit
                spikes = {
                    "tick": row["tick"],
                    "t": row["wall_time"],
                    "window_ms": settings.neural_ms,
                    "decision": neural["side"],
                    "stimulus": kind,
                    "total_spikes": int(counts.sum()),
                    "active_cells": int(np.count_nonzero(counts)),
                    "top": [[int(i), int(counts[i])] for i in top],
                    "reward": [[int(i), int(counts[i])] for i in np.asarray(circ["reward"])],
                    "aversive": [[int(i), int(counts[i])] for i in np.asarray(circ["aversive"])],
                    "kc_spikes": int(neural.get("KC_spikes", 0)),
                    "left_hz": neural.get("left_hz"),
                    "right_hz": neural.get("right_hz"),
                    "gate_spikes": neural.get("gate_spikes"),
                    "plastic_edges_changed": neural["memory"]["changed_edges"],
                    "mean_efficacy": neural["memory"].get("mean_efficacy"),
                }
                tmp = out / "spikes.json.tmp"
                tmp.write_text(json.dumps(spikes, separators=(",", ":")))
                tmp.replace(out / "spikes.json")
            except Exception:
                pass
            print(
                json.dumps(
                    {
                        "tick": row["tick"],
                        "side": neural["side"],
                        "execution": order["status"],
                        "equity": str(equity),
                        "stimulus": kind,
                        "plastic_edges_changed": neural["memory"]["changed_edges"],
                    }
                ),
                flush=True,
            )
            count += 1
            if not a.fast and (not a.steps or count < a.steps):
                until = started + settings.interval_seconds
                while time.monotonic() < until and not (out / "STOP").exists():
                    time.sleep(min(1, until - time.monotonic()))
    except KeyboardInterrupt:
        print("Stopped; run state preserved.", flush=True)
    except Exception as e:
        # Never print SDK exception text: it may contain account/request details.
        if not ledger.get("halted"):
            ledger.halt(type(e).__name__)
        frames = traceback.extract_tb(e.__traceback__)
        origin = frames[-1] if frames else None
        internal = origin and Path(origin.filename).is_relative_to(
            Path(__file__).parent
        )
        diagnostic = {
            "type": type(e).__name__,
            "reason": str(e)
            if internal
            else "External dependency error; review connection and account state.",
            "locations": [
                f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames
            ],
        }
        (out / "error.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        print(
            f"Stopped safely: {type(e).__name__}. Inspect local state and reconcile before restarting.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    finally:
        ledger.close()
        lock.close()


if __name__ == "__main__":
    main()
