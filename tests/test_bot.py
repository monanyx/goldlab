"""Bot: config safety, risk rules, sizing, signal content, kill switch, modes, feeds, broker."""

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from goldbot import http
from goldbot.bot import Bot
from goldbot.brokers.base import BrokerError, BrokerOrder
from goldbot.brokers.capital import DEMO_BASE_URL, CapitalComDemo
from goldbot.config import LIVE_CONFIRM_VALUE, BotConfig, ConfigError
from goldbot.feeds import FrameFeed, TwelveDataFeed
from goldbot.notify import ConsoleNotifier, TelegramNotifier
from goldbot.state import load
from goldbot.strategies import pick_from_report, resolve
from goldlab.engine import Engine, EngineState, Order
from goldlab.synthetic import random_walk_bars


def cfg_for(tmp_path, **kw):
    cfg = BotConfig.load(None, {})
    return replace(cfg, state_dir=str(tmp_path / "state"), history_bars=1500, **kw)


@pytest.fixture(scope="module")
def raw():
    return random_walk_bars("2024-01-01", "2024-02-10", minutes=15, seed=5)


# ----------------------------------------------------------------------------- config / safety


def test_live_mode_requires_flag_and_env(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('mode = "live"\n')
    with pytest.raises(ConfigError, match="live_trading_enabled"):
        BotConfig.load(p, {})
    p.write_text('mode = "live"\nlive_trading_enabled = true\n')
    with pytest.raises(ConfigError, match="GOLDBOT_LIVE_CONFIRM"):
        BotConfig.load(p, {})
    with pytest.raises(ConfigError, match="GOLDBOT_LIVE_CONFIRM"):
        BotConfig.load(p, {"GOLDBOT_LIVE_CONFIRM": "yes"})
    cfg = BotConfig.load(p, {"GOLDBOT_LIVE_CONFIRM": LIVE_CONFIRM_VALUE})
    assert cfg.live
    with pytest.raises(ConfigError, match="forbidden"):
        BotConfig.load(p, {"GOLDBOT_LIVE_CONFIRM": LIVE_CONFIRM_VALUE, "GOLDBOT_FORBID_LIVE": "1"})


def test_default_mode_is_signal_and_live_off():
    cfg = BotConfig.load("goldbot.toml", {})
    assert cfg.mode == "signal" and not cfg.live_trading_enabled and not cfg.kill_switch


def test_hard_limits_cannot_be_loosened(tmp_path):
    for line, msg in [("daily_loss_limit_pct = 0.05", "3%"), ("max_trades_per_day = 5", "max 3"),
                      ("risk_pct = 0.05", "risk_pct")]:
        p = tmp_path / "c.toml"
        p.write_text(line + "\n")
        with pytest.raises(ConfigError, match=msg):
            BotConfig.load(p, {})


def test_secrets_only_from_env(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('telegram_bot_token = "123:abc"\n')
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        BotConfig.load(p, {})
    cfg = BotConfig.load(None, {"TELEGRAM_BOT_TOKEN": "t", "TWELVEDATA_API_KEY": "k"})
    assert cfg.telegram_bot_token == "t" and cfg.redacted()["twelvedata_api_key"] == "set"
    assert "t" not in repr(cfg).split("telegram_bot_token")[0][-3:]


def test_kill_switch_env_override():
    assert BotConfig.load(None, {"GOLDBOT_KILL_SWITCH": "true"}).kill_switch


# ----------------------------------------------------------------------------- risk rules & sizing


def _engine(**risk):
    cfg = replace(BotConfig.load(None, {}), **risk)
    return Engine(cfg.cost_config(), cfg.risk_config())


def test_position_size_is_one_percent_of_balance_including_costs():
    eng = _engine()
    od = Order(side=1, kind="stop", price=2000.0, stop_dist=10.0, target_r=2.0)
    p = eng.plan(od, 2000.0, 100.0, 10_000.0, "London")
    # per-oz loss = 10 + spread 0.35 + 2 x slippage 0.05 = 10.45 -> floor(100/10.45) = 9 oz
    assert p.units == 9
    assert p.risk_usd == pytest.approx(9 * 10.45)
    assert p.risk_usd <= 100
    assert p.stop == 1990 and p.target == pytest.approx(2020)


def test_size_rounds_down_never_up_and_zero_when_too_small():
    eng = _engine()
    od = Order(side=-1, stop_dist=200.0)
    assert eng.plan(od, 2000.0, 100.0, 10_000.0, "London") == "size_zero"


def test_order_without_stop_cannot_exist():
    with pytest.raises(ValueError):
        Order(side=1)
    with pytest.raises(BrokerError):
        BrokerOrder(side=1, size=1, kind="market", stop_level=None)
    with pytest.raises(BrokerError):
        BrokerOrder(side=1, size=1, kind="stop", level=2000, stop_level=2010)  # wrong side


def test_cutoff_blocks_entries_after_20_vienna():
    eng = _engine()
    st = EngineState(equity=10_000, day_start_equity=10_000)
    assert eng.entry_block_reason(st, pd.Timestamp("2024-01-09 18:45", tz="UTC")) is None  # 19:45 Vienna
    assert eng.entry_block_reason(st, pd.Timestamp("2024-01-09 19:00", tz="UTC")) == "after_cutoff"
    assert eng.entry_block_reason(st, pd.Timestamp("2024-07-09 18:00", tz="UTC")) == "after_cutoff"  # CEST


def test_daily_loss_and_max_trades_rules():
    eng = _engine()
    st = EngineState(equity=9_700, day_start_equity=10_000, day_pnl=-300)
    assert eng.entry_block_reason(st, pd.Timestamp("2024-01-09 10:00", tz="UTC")) == "daily_loss_limit"
    st = EngineState(equity=10_000, day_start_equity=10_000, day_trades=3)
    assert eng.entry_block_reason(st, pd.Timestamp("2024-01-09 10:00", tz="UTC")) == "max_trades"


def test_engine_state_json_roundtrip():
    st = EngineState(equity=1.0, pending=[Order(side=1, kind="stop", price=2.0, stop_dist=0.5,
                                                expires=pd.Timestamp("2024-01-09 11:00", tz="UTC"))])
    back = EngineState.from_dict(json.loads(json.dumps(st.to_dict())))
    assert back.pending[0].expires == st.pending[0].expires and back.equity == 1.0


# ----------------------------------------------------------------------------- strategy selection


def test_auto_strategy_falls_back_to_london_breakout(tmp_path):
    assert pick_from_report(tmp_path / "missing.json")[0] == "london_breakout"
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"vwap_fade": {"verdict": "NO EDGE", "stats": {}}}))
    assert pick_from_report(p)[0] == "london_breakout"
    p.write_text(json.dumps({
        "vwap_fade": {"verdict": "EDGE AFTER COSTS (tentative)", "stats": {"Walk-forward OOS": {"expectancy_r": 0.1}}},
        "ny_momentum": {"verdict": "EDGE AFTER COSTS (tentative)", "stats": {"Walk-forward OOS": {"expectancy_r": 0.2}}},
    }))
    assert pick_from_report(p)[0] == "ny_momentum"


def test_custom_strategy_plugin(tmp_path):
    cfg = replace(BotConfig.load(None, {}), strategy="goldlab.strategies.vwap_fade:VWAPFade",
                  strategy_params={"dev_atr": 0.5})
    s, _ = resolve(cfg)
    assert s.name == "vwap_fade" and s.dev_atr == 0.5


# ----------------------------------------------------------------------------- signal content


def _run_until_signal(cfg, raw, notifier, start_bar=1600):
    feed = FrameFeed(raw, 15, cfg.history_bars)
    for i in range(start_bar, len(raw)):
        now = raw.index[i] + pd.Timedelta(minutes=15)
        res = Bot(cfg, feed, notifier).run_once(now)
        if res.signals:
            return res
    raise AssertionError("no signal produced")


def test_signal_has_direction_entry_sl_tp_size_reason(tmp_path, raw):
    n = ConsoleNotifier(quiet=True)
    res = _run_until_signal(cfg_for(tmp_path), raw, n)
    sig = res.signals[0]
    for k in ("direction", "entry", "stop_loss", "take_profit", "size_oz", "reason"):
        assert sig[k] not in (None, "", 0), k
    text = n.sent[-1]
    assert "BUY STOP" in text and "SELL STOP" in text and "Stop loss" in text and "Take profit" in text
    assert "Reason:" in text and "1.00% of $10,000" not in text  # sized net of rounding, shown as %
    assert "% of $10,000" in text
    # risk never exceeds 1% of the configured balance
    assert all(s["risk_usd"] <= 100.0 + 1e-9 for s in res.signals)
    assert (tmp_path / "state" / "signals.csv").exists()


def test_signal_mode_never_writes_paper_log(tmp_path, raw):
    cfg = cfg_for(tmp_path)
    _run_until_signal(cfg, raw, ConsoleNotifier(quiet=True))
    assert not (tmp_path / "state" / "paper_trades.csv").exists()


def test_kill_switch_config_suppresses_signals(tmp_path, raw):
    cfg = cfg_for(tmp_path, kill_switch=True)
    feed = FrameFeed(raw, 15, cfg.history_bars)
    n = ConsoleNotifier(quiet=True)
    for i in range(1600, len(raw), 3):
        res = Bot(cfg, feed, n).run_once(raw.index[i] + pd.Timedelta(minutes=15))
        assert not res.signals and res.halted
    assert not load(cfg.state_dir, 0).engine.trades


class FakeTelegram(ConsoleNotifier):
    def __init__(self):
        super().__init__(quiet=True)
        self.queue = []

    def commands(self, offset):
        cmds, self.queue = self.queue, []
        return cmds, offset + len(cmds)


def test_telegram_stop_halts_and_resume_restarts(tmp_path, raw):
    cfg = cfg_for(tmp_path, mode="paper")
    feed = FrameFeed(raw, 15, cfg.history_bars)
    tg = FakeTelegram()
    now = raw.index[1700] + pd.Timedelta(minutes=15)
    Bot(cfg, feed, tg).run_once(now)
    tg.queue = ["/stop"]
    res = Bot(cfg, feed, tg).run_once(now + pd.Timedelta(minutes=15))
    assert res.halted and load(cfg.state_dir, 0).halted
    assert any("HALTED" in m for m in tg.sent)
    for k in range(2, 200):  # stays halted across runs: no signals, no positions
        res = Bot(cfg, feed, tg).run_once(now + pd.Timedelta(minutes=15 * k))
        assert not res.signals
        assert load(cfg.state_dir, 0).engine.pos is None
    tg.queue = ["/resume"]
    assert not Bot(cfg, feed, tg).run_once(now + pd.Timedelta(minutes=15 * 200)).halted


def test_telegram_commands_only_from_configured_chat():
    def fake(method, url, **kw):
        body = {"ok": True, "result": [
            {"update_id": 5, "message": {"chat": {"id": 999}, "text": "/stop"}},
            {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/stop@goldbot"}},
            {"update_id": 7, "message": {"chat": {"id": 42}, "text": "hello"}}]}
        return http.Response(200, {}, json.dumps(body).encode())
    cmds, off = TelegramNotifier("tok", "42", req=fake).commands(0)
    assert cmds == ["/stop"] and off == 8


def test_paper_mode_logs_trades_and_daily_summary(tmp_path, raw):
    cfg = cfg_for(tmp_path, mode="paper")
    feed = FrameFeed(raw, 15, cfg.history_bars)
    n = ConsoleNotifier(quiet=True)
    for i in range(1500, len(raw), 2):
        Bot(cfg, feed, n).run_once(raw.index[i] + pd.Timedelta(minutes=17))
    st = load(cfg.state_dir, 0)
    assert len(st.engine.trades) > 3
    log = pd.read_csv(tmp_path / "state" / "paper_trades.csv")
    assert len(log) == len(st.engine.trades) and log["stop"].notna().all()
    summaries = [m for m in n.sent if "daily summary" in m]
    assert summaries and "Paper equity" in summaries[-1] and "Rules triggered" in summaries[-1]
    # rules: never more than 3 trades a day, every trade has a stop, nothing opened after 20:00 Vienna
    t = pd.DataFrame([x.to_dict() for x in st.engine.trades])
    assert t.groupby("trade_date").size().max() <= 3
    local = pd.to_datetime(t["entry_time"], utc=True).dt.tz_convert("Europe/Vienna")
    assert ((local.dt.hour * 60 + local.dt.minute) < 20 * 60).all()


def test_summary_sent_once_per_day(tmp_path, raw):
    cfg = cfg_for(tmp_path)
    feed = FrameFeed(raw, 15, cfg.history_bars)
    n = ConsoleNotifier(quiet=True)
    day = pd.Timestamp("2024-01-30 21:31", tz="UTC")  # 22:31 Vienna
    for k in range(4):
        Bot(cfg, feed, n).run_once(day + pd.Timedelta(minutes=15 * k))
    assert sum("daily summary" in m for m in n.sent) == 1


# ----------------------------------------------------------------------------- feeds & errors


def test_twelvedata_parsing_drops_forming_bar():
    def fake(method, url, params=None, **kw):
        assert params["symbol"] == "XAU/USD" and params["timezone"] == "UTC" and params["apikey"] == "k"
        body = {"status": "ok", "values": [
            {"datetime": "2024-01-09 10:00:00", "open": "2030", "high": "2031", "low": "2029", "close": "2030.5"},
            {"datetime": "2024-01-09 10:15:00", "open": "2030.5", "high": "2032", "low": "2030", "close": "2031"}]}
        return http.Response(200, {}, json.dumps(body).encode())
    df = TwelveDataFeed("k", get=fake).bars(pd.Timestamp("2024-01-09 10:20", tz="UTC"))
    assert list(df.index) == [pd.Timestamp("2024-01-09 10:00", tz="UTC")]
    assert df["close"].iloc[0] == 2030.5


def test_twelvedata_error_raises():
    def fake(method, url, **kw):
        return http.Response(200, {}, b'{"status":"error","code":429,"message":"rate limit"}')
    with pytest.raises(Exception, match="rate limit"):
        TwelveDataFeed("k", get=fake).bars(pd.Timestamp.now(tz="UTC"))


def test_feed_failure_alerts_and_raises(tmp_path):
    class Broken:
        name = "broken"
        def bars(self, now):
            raise RuntimeError("down")
    n = ConsoleNotifier(quiet=True)
    with pytest.raises(RuntimeError):
        Bot(cfg_for(tmp_path), Broken(), n).run_once(pd.Timestamp("2024-01-09 10:00", tz="UTC"))
    assert "data feed failed" in n.sent[-1]


# ----------------------------------------------------------------------------- broker adapter (mocked HTTP)


class FakeCapital:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers=None, json_body=None, params=None, **kw):
        self.calls.append((method, url, json_body))
        assert url.startswith(DEMO_BASE_URL)
        path = url[len(DEMO_BASE_URL):]
        if path == "/api/v1/session":
            assert headers["X-CAP-API-KEY"] == "key"
            return http.Response(200, {"CST": "c", "X-SECURITY-TOKEN": "s"}, b"{}")
        assert headers["CST"] == "c" and headers["X-SECURITY-TOKEN"] == "s"
        if path == "/api/v1/accounts":
            body = {"accounts": [{"preferred": True, "balance": {"balance": 10000.0}}]}
        elif path == "/api/v1/markets/GOLD":
            body = {"dealingRules": {"minDealSize": {"value": 0.01}}}
        elif path in ("/api/v1/workingorders", "/api/v1/positions") and method == "POST":
            body = {"dealReference": "o_1"}
        elif path == "/api/v1/confirms/o_1":
            body = {"dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "D1"}], "status": "OPEN"}
        elif path == "/api/v1/positions":
            body = {"positions": [{"position": {"dealId": "P1", "direction": "BUY", "size": 1, "level": 2000,
                                                "upl": -5}, "market": {"epic": "GOLD"}}]}
        elif path == "/api/v1/workingorders":
            body = {"workingOrders": [{"workingOrderData": {"dealId": "W1", "direction": "SELL", "epic": "GOLD",
                                                            "orderSize": 1, "orderLevel": 1990}}]}
        else:
            body = {}
        return http.Response(200, {}, json.dumps(body).encode())


def test_capital_adapter_demo_only_and_order_shape():
    fake = FakeCapital()
    b = CapitalComDemo("key", "me@x.com", "pw", req=fake)
    assert b.base == DEMO_BASE_URL and "demo" in b.base
    assert b.balance() == 10000.0
    assert b.round_size(3.4567) == 3.45
    r = b.place(BrokerOrder(side=1, size=3.45, kind="stop", level=2031.234, stop_level=2018.1,
                            profit_level=2050.0, good_till=pd.Timestamp("2024-01-09 11:00", tz="UTC")))
    assert r["deal_id"] == "D1"
    body = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/workingorders")][0][2]
    assert body == {"epic": "GOLD", "direction": "BUY", "size": 3.45, "guaranteedStop": False,
                    "stopLevel": 2018.1, "profitLevel": 2050.0, "level": 2031.23, "type": "STOP",
                    "goodTillDate": "2024-01-09T11:00:00"}
    assert b.close_all() == 2
    assert ("DELETE", f"{DEMO_BASE_URL}/api/v1/workingorders/W1", None) in fake.calls
    assert ("DELETE", f"{DEMO_BASE_URL}/api/v1/positions/P1", None) in fake.calls


def test_capital_relogin_on_401():
    fake = FakeCapital()
    state = {"n": 0}

    def flaky(method, url, **kw):
        if url.endswith("/accounts") and state["n"] == 0:
            state["n"] += 1
            raise http.HttpError(401, "expired", url)
        return fake(method, url, **kw)
    assert CapitalComDemo("key", "me", "pw", req=flaky).balance() == 10000.0
    assert sum(1 for c in fake.calls if c[1].endswith("/session")) == 2


# ----------------------------------------------------------------------------- live mode with a fake broker


class FakeBroker:
    name = "fake"

    def __init__(self):
        self.placed, self.closed_all, self.pos, self.orders = [], 0, [], []

    def balance(self):
        return 10_000.0

    def round_size(self, units):
        return float(int(units))

    def place(self, order):
        self.placed.append(order)
        self.orders.append({"deal_id": f"W{len(self.placed)}", "side": order.side})
        return {"deal_id": f"W{len(self.placed)}"}

    def positions(self):
        return self.pos

    def working_orders(self):
        return self.orders

    def cancel_order(self, deal_id):
        self.orders = [o for o in self.orders if o["deal_id"] != deal_id]

    def close_position(self, deal_id):
        self.pos = []

    def close_all(self):
        self.closed_all += 1
        n = len(self.orders) + len(self.pos)
        self.orders, self.pos = [], []
        return n


def test_live_mode_sends_orders_with_stops_and_kill_switch_flattens(tmp_path, raw):
    cfg = cfg_for(tmp_path, mode="live", live_trading_enabled=True)
    broker = FakeBroker()
    n = ConsoleNotifier(quiet=True)
    feed = FrameFeed(raw, 15, cfg.history_bars)
    for i in range(1600, len(raw)):
        if Bot(cfg, feed, n, broker).run_once(raw.index[i] + pd.Timedelta(minutes=15)).signals:
            break
    assert broker.placed and all(o.stop_level > 0 for o in broker.placed)
    assert {o.side for o in broker.placed} == {1, -1}  # OCO pair
    tg = FakeTelegram()
    tg.queue = ["/stop"]
    Bot(cfg, feed, tg, broker).run_once(raw.index[i + 1] + pd.Timedelta(minutes=15))
    assert broker.closed_all >= 1 and not broker.orders


def test_live_mode_refuses_without_broker(tmp_path, raw):
    with pytest.raises(ValueError):
        Bot(cfg_for(tmp_path, mode="live"), FrameFeed(raw), ConsoleNotifier(quiet=True))
