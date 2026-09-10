"""Live execution on Pons V2 bonding curves (Robinhood Chain) from the fly's own wallet.

Same contract as CoinbaseBroker: preflight / verify_balances / reconcile / execute.
Every order is a single on-chain tx (plus an approve before the first sell of a coin):
  BUY : curve.buy(quoteIn, minTokensOut, wallet)  value = quoteIn
  SELL: curve.sell(tokensIn, minQuoteOut, wallet)
minOut is derived from the plan's limit price, so a fill can never be worse than the
ledger's limit. Fills are settled from actual balance deltas, gas booked as the fee.
The tx hash is persisted before broadcast so a crash mid-send can be reconciled.
"""

import os
import time

from .config import D
from .pons import WEI, Pons, Signer, approve_calldata, buy_calldata, sell_calldata
from .pons_v4 import ETH, ROUTER, approve_router_calldata, swap_calldata
from .risk import Veto

LIVE_OPT_IN = "I_ACCEPT_REAL_TRADES"


class UnresolvedOrder(RuntimeError):
    pass


class PonsBroker:
    mode = "live"

    def __init__(self, settings, ledger, market, signer, gas_reserve="0.0005"):
        self.s = settings
        self.l = ledger
        self.m = market
        self.signer = signer
        self.pons = market.pons
        self.address = signer.address
        self.gas_reserve = D(gas_reserve)
        market.wallet = self.address

    @classmethod
    def from_env(cls, settings, ledger, market):
        if os.environ.get("STONKFLY_LIVE") != LIVE_OPT_IN:
            raise RuntimeError("Live opt-in missing (STONKFLY_LIVE=I_ACCEPT_REAL_TRADES)")
        return cls(settings, ledger, market, Signer(market.pons.rpc), os.environ.get("HOODFLY_GAS_RESERVE", "0.0005"))

    # ---- balances ----------------------------------------------------------------------
    def eth(self):
        return D(self.pons.eth_balance(self.address)) / WEI

    def tokens(self, product):
        coin = product.split("-", 1)[1]
        return D(self.pons.token_balance(coin, self.address)) / WEI

    def preflight(self):
        self.reconcile()
        bal = self.eth()
        if not self.l.get("live_initialized"):
            if self.l.get("tick") or self.l.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
                raise RuntimeError("Uninitialized live ledger already has activity")
            cash = bal - self.gas_reserve
            if not 0 < cash <= D(self.s.capital):
                raise RuntimeError(f"Fund the fly wallet with more than the gas reserve and at most {self.s.capital} ETH (have {bal})")
            with self.l.transaction():
                for k in ["cash", "initial_cash", "anchor"]:
                    self.l.put(k, str(cash))
                self.l.put("live_initialized", True)
                self.l.put("wallet", self.address)
        self.verify_balances()
        return {"mode": "live", "venue": "pons/robinhood-chain", "wallet": self.address, "eth": str(bal), "withdrawals_possible": False}

    def book_fee_income(self):
        """Richy's treasury pays the fly its share of LP fees. Book each Harvested event as a
        deposit (cash, initial and anchor all rise) so it is capital, never counted as PnL."""
        treasury = os.environ.get("RICHY_TREASURY")
        if not treasury:
            return
        from web3 import Web3
        topic = "0x" + Web3.keccak(text="Harvested(uint256,uint256,uint256)").hex().replace("0x", "")
        head = self.pons.rpc.block_number()
        frm = int(self.l.get("fees_scanned") or 0)
        if frm == 0:
            frm = head - 1  # start from now; earlier fees predate this ledger
        total = 0
        try:
            for b in range(frm + 1, head + 1, 9000):
                logs = self.pons.rpc.call("eth_getLogs", [{"address": treasury, "topics": [topic], "fromBlock": hex(b), "toBlock": hex(min(head, b + 8999))}]) or []
                for l in logs:
                    total += int(l["data"][2:66], 16)
        except Exception:
            return  # try again next tick; nothing booked
        if total:
            amt = D(total) / WEI
            with self.l.transaction():
                for k in ("cash", "initial_cash", "anchor"):
                    self.l.put(k, str(D(self.l.get(k)) + amt))
                self.l.put("fee_income_eth", str(D(self.l.get("fee_income_eth") or "0") + amt))
        self.l.put("fees_scanned", head)

    def verify_balances(self):
        self.book_fee_income()
        dust = {k: D(v) for k, v in (self.l.get("dust") or {}).items()}
        actual = self.eth() - self.gas_reserve
        if abs(actual - self.l.cash) > D("0.0003"):
            raise RuntimeError("External ETH balance change; stop and reconcile rather than treat deposits as profit")
        for p, amount in self.l.positions.items():
            if amount <= 0:
                continue
            if abs(self.tokens(p) - amount - dust.get(p, D(0))) > D("0.001"):
                raise RuntimeError(f"External token balance change on {p.split('-')[0]}; reconcile before trading")

    # ---- execution -----------------------------------------------------------------------
    def execute(self, p, before_submit):
        cid = p["client_order_id"]
        product, side = p["product"], p["side"]
        meta = self.m.meta.get(product)
        state = self.m.states.get(product)
        try:
            if not meta or not state:
                raise Veto("Coin left the fly's view before execution")
            v4 = bool(state["graduated"])  # graduated: trade in the Pons V4 pool via the Pons router
            if time.time() - p["quote_timestamp"] > self.s.max_quote_age:
                raise Veto("Quote expired during preparation")
            coin, curve = meta["coin"], meta["curve"]
            base = D(p["base_size"])
            limit = D(p["limit_price"])
            pre_eth = self.pons.eth_balance(self.address)
            pre_tok = self.pons.token_balance(coin, self.address)
            approve_gas = 0
            target = ROUTER if v4 else curve
            if side == "BUY":
                quote_in = int(base * D(p["observed_ask"]) * WEI)
                if not v4 and quote_in > int(0.0145 * state["quote_reserve"]):
                    raise Veto("Order too large for this curve's price-impact cap")
                if quote_in + int(self.gas_reserve * WEI) > pre_eth:
                    raise Veto("Insufficient ETH for order plus gas reserve")
                min_out = int(D(quote_in) / limit) + 1
                data = swap_calldata(ETH, coin, quote_in, min_out) if v4 else buy_calldata(quote_in, min_out, self.address)
                value = quote_in
            else:
                tokens_in = int(base * WEI)
                if tokens_in > pre_tok:
                    raise Veto("Ledger position exceeds wallet balance")
                min_out = int(D(tokens_in) * limit)
                if self.pons.allowance(coin, self.address, target) < tokens_in:
                    approve_gas = self._approve(coin, target)
                data = swap_calldata(coin, ETH, tokens_in, min_out) if v4 else sell_calldata(tokens_in, min_out, self.address)
                value = 0
            try:
                gas = self.signer.estimate(target, data, value)  # reverts here cost nothing
            except Exception as e:  # chain refused the order (tax window, price moved, impact cap): skip, never crash
                try:
                    with open(os.path.join(os.path.dirname(str(self.l.path)), "vetoes.log"), "a") as f:
                        f.write(f"{time.time():.0f} {side} {product} {type(e).__name__} {str(e)[:300]}\n")
                except Exception:
                    pass
                raise Veto(f"Chain rejected the order ({type(e).__name__})")
            self.verify_balances()
            before_submit(p)
            raw, txh = self.signer.sign(target, data, value, gas)
        except Exception:
            self.l.mark(cid, "REJECTED")
            raise
        # Durable UNKNOWN + hash precede the broadcast.
        with self.l.transaction():
            self.l.mark(cid, "UNKNOWN", txh)
            self.l.put("inflight", {"cid": cid, "tx": txh, "pre_eth": str(pre_eth), "pre_tok": str(pre_tok), "coin": coin, "approve_gas": str(approve_gas), "value": str(value)})
        try:
            self.signer.broadcast(raw)
        except Exception as e:
            raise UnresolvedOrder("Broadcast outcome unknown; reconcile before any further trade") from e
        self.l.mark(cid, "ACCEPTED", txh)
        if self._settle(cid, p, self.l.get("inflight")):
            return {"mode": "live", "status": "SETTLED", "client_order_id": cid, "tx": txh}
        raise UnresolvedOrder("Transaction not mined within the wait window; live execution stopped")

    def _approve(self, coin, spender):
        txh = self.signer.send(coin, approve_calldata(spender, 2**256 - 1))
        r = self.signer.receipt(txh, 90)
        if r is None or r["status"] != 1:
            raise Veto("Token approve failed")
        return int(r["gasUsed"]) * int(r.get("effectiveGasPrice", 0))

    def _settle(self, cid, p, inflight):
        r = self.signer.receipt(inflight["tx"], 90)
        if r is None:
            return False
        gas = int(r["gasUsed"]) * int(r.get("effectiveGasPrice", 0)) + int(inflight.get("approve_gas", 0))
        fee = D(gas) / WEI
        coin = inflight["coin"]
        pre_eth, pre_tok = int(inflight["pre_eth"]), int(inflight["pre_tok"])
        post_eth = self.pons.eth_balance(self.address)
        post_tok = self.pons.token_balance(coin, self.address)
        if r["status"] != 1:
            # reverted on chain: gas was still burned, book it as a pure loss
            with self.l.transaction():
                self.l.mark(cid, "REJECTED")
                self.l.put("cash", str(self.l.cash - fee))
                self.l.put("inflight", None)
            return True
        if p["side"] == "BUY":
            got = D(post_tok - pre_tok) / WEI
            quote = D(int(inflight["value"])) / WEI
            base = min(got, D(p["base_size"]))
            if got > base:  # tiny overfill (price moved in our favour); tracked so balances still reconcile
                dust = self.l.get("dust") or {}
                dust[p["product"]] = str(D(dust.get(p["product"], "0")) + (got - base))
                self.l.put("dust", dust)
        else:
            base = D(pre_tok - post_tok) / WEI
            quote = D(post_eth - pre_eth + gas) / WEI
        self.l.settle(cid, base, quote, fee)
        self.l.put("inflight", None)
        return True

    def reconcile(self):
        inflight = self.l.get("inflight")
        for row in self.l.pending():
            cid, p = row["id"], row["plan"]
            if row["status"] == "PREPARED":
                self.l.mark(cid, "REJECTED")
                continue
            txh = row["exchange_id"] or (inflight or {}).get("tx")
            if not txh or not inflight or inflight.get("cid") != cid:
                raise UnresolvedOrder("Order in unknown state without a tx hash; inspect the wallet on rh-scan and clear manually")
            if not self._settle(cid, p, inflight):
                raise UnresolvedOrder("Broadcast tx still unmined; wait and rerun")
