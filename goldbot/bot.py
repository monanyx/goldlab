"""One bot cycle: commands -> data -> strategy -> engine (risk rules) -> alerts / orders -> state.

The decision path is the goldlab `Engine`, stepped one completed bar at a
time with its state persisted between runs. It is the same code the
backtester runs, so paper results match the backtest bar for bar (see
`goldbot.replay`).

Modes:
  signal  alerts only (the engine still runs in the background so the daily
          loss limit and trade count can be enforced on hypothetical results)
  paper   alerts + every signal becomes a virtual trade; P&L is tracked
  live    alerts + orders on the Capital.com DEMO account (needs config flag
          AND environment confirmation)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

import pandas as pd

from goldlab.data import clean
from goldlab.engine import Engine, Event, Order, OrderPlan, bars_from_frame

from . import state as statefile
from .brokers.base import Broker, BrokerError, BrokerOrder
from .config import BotConfig
from .feeds import Feed
from .notify import Notifier
from .strategies import resolve

log = logging.getLogger("goldbot")

RULE_TEXT = {
    "daily_loss_limit": "Daily loss limit reached: no more trades today",
    "max_trades": "Max trades per day reached: no more trades today",
    "after_cutoff": "No new trades after the daily cut-off time",
    "kill_switch": "Kill switch active: everything halted",
    "size_zero": "Position size rounds to zero at 1% risk: trade skipped",
    "invalid_levels": "Price gapped through the stop before entry: trade skipped",
    "stale_signal": "Signal is older than one bar (late run): not sent to the broker",
}
MIN_BARS = 200


@dataclass
class RunResult:
    processed_bars: int = 0
    signals: list = field(default_factory=list)
    events: list = field(default_factory=list)
    halted: bool = False
    summary_sent: bool = False


def fmt_lots(units: float) -> str:
    return f"{units:g} oz ({units / 100:g} lot)"


class Bot:
    def __init__(self, cfg: BotConfig, feed: Feed, notifier: Notifier, broker: Broker | None = None):
        if cfg.live and broker is None:
            raise ValueError("live mode needs a broker")
        self.cfg, self.feed, self.notify, self.broker = cfg, feed, notifier, broker
        self.strategy, self.strategy_why = resolve(cfg)
        self.engine = Engine(cfg.cost_config(), cfg.risk_config())
        self.tag = {"signal": "SIGNAL", "paper": "PAPER", "live": "LIVE-DEMO"}[cfg.mode]

    # ------------------------------------------------------------------ public

    def run_once(self, now: pd.Timestamp | None = None) -> RunResult:
        now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")
        cfg = self.cfg
        st = statefile.load(cfg.state_dir, cfg.account_balance)
        res = RunResult()
        if st.mode and st.mode != cfg.mode:
            self.notify.send(f"goldbot: mode changed {st.mode} -> {cfg.mode}")
        st.mode, st.strategy = cfg.mode, self.strategy.name

        self._commands(st, now)
        halted = cfg.kill_switch or st.halted
        res.halted = halted
        if halted and self.broker is not None:
            self._broker_flatten("kill switch")

        try:
            raw = self.feed.bars(now)
            df, _ = clean(raw, spike_threshold=None)
            if len(df) < MIN_BARS:
                raise RuntimeError(f"only {len(df)} bars of history (need >= {MIN_BARS})")
        except Exception as e:  # noqa: BLE001
            self._error_alert(st, now, f"data feed failed: {e}")
            statefile.save(cfg.state_dir, st)
            raise

        sig = self.strategy.signals(df)
        bars = bars_from_frame(df, sig.flat, cfg.timeframe_minutes)
        last = st.engine.last_time
        if last is None:
            todo = [len(bars) - 1]  # first run: start from the latest completed bar
            self.notify.send(f"goldbot started in {cfg.mode.upper()} mode, strategy {self.strategy.name} "
                             f"({self.strategy_why}). Balance basis ${cfg.account_balance:,.0f}.")
        else:
            todo = [i for i, b in enumerate(bars) if b.time > last]

        for i in todo:
            bar = bars[i]
            orders = sig.orders.get(i)
            if halted:
                bar = replace(bar, flat=True)
                orders = None
            fresh = i == len(bars) - 1
            events, _ = self.engine.step(st.engine, bar, orders)
            res.events.extend(events)
            for ev in events:
                self._handle(st, ev, bar, fresh, now, res)
            res.processed_bars += 1

        if self.broker is not None and bars:
            self._live_housekeeping(st, bars[-1], halted)

        if cfg.mode == "paper":
            statefile.write_paper_trades(cfg.state_dir, st)
        if bars:
            res.summary_sent = self._maybe_summary(st, now, bars[-1].trade_date)
        statefile.save(cfg.state_dir, st)
        return res

    # ------------------------------------------------------------------ commands / kill switch

    def _commands(self, st, now) -> None:
        cmds, st.telegram_offset = self.notify.commands(st.telegram_offset)
        for c in cmds:
            if c == "/stop":
                st.halted = True
                self.notify.send("🛑 /stop received: goldbot HALTED. Pending orders cancelled, positions "
                                 "closed, no new trades. Send /resume to restart.")
            elif c == "/resume":
                if self.cfg.kill_switch:
                    self.notify.send("Cannot resume: kill_switch = true in the config file.")
                else:
                    st.halted = False
                    self.notify.send("▶️ /resume received: goldbot running again.")
            elif c == "/status":
                self.notify.send(self._status_text(st))
            elif c == "/help":
                self.notify.send("Commands: /stop (halt everything), /resume, /status")

    def _status_text(self, st) -> str:
        e = st.engine
        pos = "none" if e.pos is None else (f"{'LONG' if e.pos.side == 1 else 'SHORT'} {fmt_lots(e.pos.units)} "
                                            f"@ {e.pos.entry_price:.2f}, SL {e.pos.stop:.2f}")
        halted = self.cfg.kill_switch or st.halted
        return (f"goldbot status [{self.tag}]\nHalted: {'YES' if halted else 'no'}\n"
                f"Strategy: {self.strategy.name}\nLast bar: {e.last_time}\n"
                f"Trades today: {e.day_trades}/{self.cfg.max_trades_per_day}\n"
                f"Day P&L ({'paper' if self.cfg.mode != 'live' else 'shadow'}): ${e.day_pnl:+,.2f}\n"
                f"Pending orders: {len(e.pending)}\nOpen position: {pos}")

    # ------------------------------------------------------------------ events

    def _handle(self, st, ev: Event, bar, fresh: bool, now, res: RunResult) -> None:
        if ev.kind == "orders":
            self._new_orders(st, ev.orders, bar, fresh, now, res)
        elif ev.kind == "blocked":
            if st.rule(bar.trade_date, ev.reason) and self.cfg.mode != "live":
                self.notify.send(f"⚠️ [{self.tag}] Rule triggered: {RULE_TEXT.get(ev.reason, ev.reason)}")
        elif ev.kind == "opened" and self.cfg.mode == "paper":
            t = ev.trade
            self.notify.send(f"📥 [PAPER] Opened {'LONG' if t.side == 1 else 'SHORT'} {fmt_lots(t.units)} "
                             f"@ {t.entry_price:.2f} | SL {t.stop:.2f} | TP "
                             f"{'-' if t.target is None else f'{t.target:.2f}'} | risk ${t.risk_usd:,.0f}")
        elif ev.kind == "closed":
            t = ev.trade
            if self.cfg.mode == "paper":
                self.notify.send(f"📤 [PAPER] Closed {'LONG' if t.side == 1 else 'SHORT'} @ {t.exit_price:.2f} "
                                 f"({t.exit_reason}) | P&L ${t.pnl_usd:+,.2f} ({t.r_multiple:+.2f}R) | "
                                 f"equity ${t.equity_after:,.2f}")
            e = st.engine
            normal, allowance = self.engine.risk_budget(e)
            if allowance < 0.25 * normal and st.rule(bar.trade_date, "daily_loss_limit"):
                self.notify.send(f"⛔ [{self.tag}] {RULE_TEXT['daily_loss_limit']} "
                                 f"(day P&L ${e.day_pnl:+,.2f}).")

    def _preview(self, st, od: Order, bar) -> tuple[OrderPlan | str, float]:
        """Size the order exactly as the engine would at fill (for the alert / broker)."""
        e = st.engine
        ref = od.price if od.kind == "stop" else bar.close
        if self.cfg.mode == "signal":
            basis = self.cfg.account_balance
            normal = allowance = self.cfg.risk_pct * basis
        elif self.cfg.mode == "live":
            basis = self.broker.balance()
            day_start = st.live.get("day_start_balance", basis)
            normal = self.cfg.risk_pct * day_start
            allowance = self.cfg.daily_loss_limit_pct * day_start + (basis - day_start)
        else:
            basis = e.equity
            normal, allowance = self.engine.risk_budget(e)
        return self.engine.plan(od, ref, min(normal, allowance), basis, bar.session), basis

    def _new_orders(self, st, orders: list[Order], bar, fresh: bool, now, res: RunResult) -> None:
        cfg = self.cfg
        next_open = bar.time + pd.Timedelta(minutes=cfg.timeframe_minutes)
        why = self.engine.entry_block_reason(st.engine, next_open)
        if why is None and st.live.get("stopped_day") == str(pd.Timestamp(bar.trade_date).date()):
            why = "daily_loss_limit"
        lines, rows, broker_orders = [], [], []
        for od in orders:
            plan, basis = (None, 0.0) if why else self._preview(st, od, bar)
            if isinstance(plan, str):
                why = why or plan
            status = "blocked:" + why if why else ("sent" if fresh else "late")
            row = {"created_utc": bar.time + pd.Timedelta(minutes=cfg.timeframe_minutes),
                   "mode": cfg.mode, "strategy": self.strategy.name,
                   "direction": "BUY" if od.side == 1 else "SELL", "order_type": od.kind,
                   "valid_until_utc": od.expires, "status": status, "reason": od.reason}
            if isinstance(plan, OrderPlan):
                row.update(entry=round(plan.entry_mid, 2), stop_loss=round(plan.stop, 2),
                           take_profit=None if plan.target is None else round(plan.target, 2),
                           size_oz=plan.units, risk_usd=round(plan.risk_usd, 2))
                lines.append(self._signal_text(od, plan, basis))
                broker_orders.append((od, plan))
            rows.append(row)
        for r in rows:
            statefile.append_signal(cfg.state_dir, r)
        if why:
            if st.rule(bar.trade_date, why):
                self.notify.send(f"⚠️ [{self.tag}] Signal suppressed — {RULE_TEXT.get(why, why)}")
            return
        st.day(bar.trade_date)["signals"] += len(lines)
        res.signals.extend(rows)
        head = f"🔔 XAUUSD {self.strategy.name} [{self.tag}]"
        if not fresh:
            head += f"\n⏱ LATE: this run is {(now - row['created_utc']).total_seconds() / 60:.0f} min after the " \
                    "signal bar; price may have moved."
        if len(lines) > 1 and orders[0].group:
            head += "\nOne-cancels-other: when one side triggers, cancel the other."
        self.notify.send(head + "\n\n" + "\n\n".join(lines))
        if cfg.live:
            if not fresh:
                st.rule(bar.trade_date, "stale_signal")
                self.notify.send(f"⚠️ [LIVE-DEMO] {RULE_TEXT['stale_signal']}")
            else:
                self._broker_submit(st, broker_orders, bar)

    def _signal_text(self, od: Order, p: OrderPlan, basis: float) -> str:
        side = "BUY" if od.side == 1 else "SELL"
        kind = f"{side} STOP @ {p.entry_mid:.2f}" if od.kind == "stop" else f"{side} at market (~{p.entry_mid:.2f})"
        tp = "none" if p.target is None else f"{p.target:.2f}"
        out = [f"{'🟢' if od.side == 1 else '🔴'} {kind}",
               f"Stop loss: {p.stop:.2f}   Take profit: {tp}",
               f"Size: {fmt_lots(p.units)} = ${p.risk_usd:,.0f} risk "
               f"({p.risk_usd / basis:.2%} of ${basis:,.0f}, incl. spread)"]
        if od.expires is not None:
            out.append(f"Valid until: {od.expires:%Y-%m-%d %H:%M} UTC")
        out.append(f"Reason: {od.reason}")
        return "\n".join(out)

    # ------------------------------------------------------------------ live (demo broker)

    def _broker_submit(self, st, items: list[tuple[Order, OrderPlan]], bar) -> None:
        cfg = self.cfg
        day = str(pd.Timestamp(bar.trade_date).date())
        if st.live.get("day") != day:
            st.live.update(day=day, orders_today=0)
        if st.live.get("orders_today", 0) >= cfg.max_trades_per_day:
            st.rule(bar.trade_date, "max_trades")
            self.notify.send(f"⚠️ [LIVE-DEMO] {RULE_TEXT['max_trades']}")
            return
        if self.broker.positions():
            return  # one position at a time
        placed = []
        for od, p in items:
            size = self.broker.round_size(p.units) if hasattr(self.broker, "round_size") else p.units
            if size <= 0:
                self.notify.send(f"⚠️ [LIVE-DEMO] {RULE_TEXT['size_zero']}")
                continue
            bo = BrokerOrder(side=od.side, size=size, kind=od.kind, stop_level=p.stop,
                             level=p.entry_mid if od.kind == "stop" else None,
                             profit_level=p.target, good_till=od.expires, group=od.group)
            try:
                r = self.broker.place(bo)
                placed.append(r["deal_id"])
                self.notify.send(f"✅ [LIVE-DEMO] order accepted: {'BUY' if od.side == 1 else 'SELL'} "
                                 f"{size:g} oz {od.kind} deal {r['deal_id']}")
            except BrokerError as e:
                self.notify.send(f"❌ [LIVE-DEMO] order failed: {e}")
        if placed:
            # an OCO pair counts as one trade (conservative: counted when placed, not when filled)
            st.live["orders_today"] = st.live.get("orders_today", 0) + 1
            st.live.setdefault("groups", {})[items[0][0].group or placed[0]] = placed

    def _broker_flatten(self, why: str) -> None:
        try:
            n = self.broker.close_all()
            if n:
                self.notify.send(f"🛑 [LIVE-DEMO] {why}: cancelled/closed {n} broker orders/positions")
        except BrokerError as e:
            self.notify.send(f"❌ [LIVE-DEMO] failed to flatten on {why}: {e}")

    def _live_housekeeping(self, st, last_bar, halted: bool) -> None:
        cfg = self.cfg
        day = str(pd.Timestamp(last_bar.trade_date).date())
        try:
            balance = self.broker.balance()
            positions = self.broker.positions()
            if st.live.get("balance_day") != day:
                st.live.update(balance_day=day, day_start_balance=balance)
            start = st.live["day_start_balance"]
            equity = balance + sum(p.get("upl", 0.0) for p in positions)
            # daily loss limit on real broker equity
            if equity - start <= -cfg.daily_loss_limit_pct * start and st.live.get("stopped_day") != day:
                st.live["stopped_day"] = day
                st.rule(last_bar.trade_date, "daily_loss_limit")
                self.notify.send(f"⛔ [LIVE-DEMO] {RULE_TEXT['daily_loss_limit']} "
                                 f"(equity {equity:,.2f} vs day start {start:,.2f})")
                self._broker_flatten("daily loss limit")
                return
            # session end / strategy flat
            if (last_bar.flat or halted) and (positions or self.broker.working_orders()):
                self._broker_flatten("session end" if not halted else "kill switch")
                return
            # emulate OCO: once a position exists, cancel the remaining legs
            if positions:
                for wo in self.broker.working_orders():
                    self.broker.cancel_order(wo["deal_id"])
        except BrokerError as e:
            self.notify.send(f"❌ [LIVE-DEMO] broker housekeeping failed: {e}")

    # ------------------------------------------------------------------ summary / errors

    def _maybe_summary(self, st, now, trade_date) -> bool:
        local = now.tz_convert(self.cfg.timezone)
        hh, mm = map(int, self.cfg.daily_summary_time.split(":"))
        today = local.strftime("%Y-%m-%d")
        if local.hour * 60 + local.minute < hh * 60 + mm or st.summary_sent_for == today:
            return False
        self.notify.send(self.summary_text(st, trade_date))
        st.summary_sent_for = today
        return True

    def summary_text(self, st, trade_date) -> str:
        e = st.engine
        day = pd.Timestamp(trade_date)
        d = st.day(day)
        lines = [f"📊 goldbot daily summary {day:%a %d %b %Y} [{self.tag}]",
                 f"Strategy: {self.strategy.name}",
                 f"Signals sent: {d['signals']}"]
        todays = [t for t in e.trades if pd.Timestamp(t.trade_date) == day]
        if self.cfg.mode == "paper":
            pnl = sum(t.pnl_usd for t in todays)
            r = sum(t.r_multiple for t in todays)
            total = e.equity - self.cfg.account_balance
            lines += [f"Paper trades closed today: {len(todays)}  P&L ${pnl:+,.2f} ({r:+.2f}R)",
                      f"Paper equity: ${e.equity:,.2f} ({total:+,.2f} since start, {len(e.trades)} trades)"]
            if e.pos is not None:
                lines.append(f"Open paper position: {'LONG' if e.pos.side == 1 else 'SHORT'} "
                             f"{fmt_lots(e.pos.units)} @ {e.pos.entry_price:.2f}")
        elif self.cfg.mode == "signal":
            lines.append(f"Hypothetical result of today's signals: ${sum(t.pnl_usd for t in todays):+,.2f} "
                         f"({len(todays)} trades, sized on the engine's shadow account)")
        else:
            lines.append(f"Broker day-start balance: {st.live.get('day_start_balance', 'n/a')}")
        rules = d["rules"]
        lines.append("Rules triggered: " + (", ".join(f"{k} x{v}" for k, v in rules.items()) or "none"))
        lines.append(f"Kill switch: {'ON' if self.cfg.kill_switch or st.halted else 'off'}")
        return "\n".join(lines)

    def _error_alert(self, st, now, msg: str) -> None:
        log.error(msg)
        last = pd.Timestamp(st.last_error_alert) if st.last_error_alert else None
        if last is None or now - last > pd.Timedelta(hours=2):
            self.notify.send(f"❗ goldbot error: {msg}")
            st.last_error_alert = now.isoformat()
