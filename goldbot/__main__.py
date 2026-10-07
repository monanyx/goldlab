"""goldbot CLI.

  python -m goldbot run              one cycle (what GitHub Actions calls every 15 min)
  python -m goldbot loop             run forever, one cycle per minute (VPS / Docker)
  python -m goldbot replay           live code path over historical CSV vs the backtest
  python -m goldbot status           print config (secrets redacted) and state
  python -m goldbot telegram-setup   show chat IDs that messaged your bot / send a test
  python -m goldbot broker-check     log in to the Capital.com DEMO account (read-only)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

from .config import BotConfig, ConfigError

log = logging.getLogger("goldbot")


def _broker(cfg):
    if not cfg.live:
        return None
    from .brokers import CapitalComDemo
    return CapitalComDemo(cfg.capital_api_key, cfg.capital_identifier, cfg.capital_password, cfg.capital_epic)


def _bot(cfg):
    from .bot import Bot
    from .feeds import make_feed
    from .notify import make_notifier
    return Bot(cfg, make_feed(cfg), make_notifier(cfg), _broker(cfg))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="goldbot")
    ap.add_argument("--config", default="goldbot.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    p_loop = sub.add_parser("loop")
    p_loop.add_argument("--interval", type=int, default=60, help="seconds between cycles")
    p_rep = sub.add_parser("replay")
    p_rep.add_argument("--data", default="data", help="CSV file or folder (same formats as goldlab)")
    p_rep.add_argument("--start", default=None)
    p_rep.add_argument("--end", default=None)
    p_rep.add_argument("--synthetic", action="store_true", help="use a synthetic random walk instead")
    p_rep.add_argument("--out", default="results/replay_check.md")
    sub.add_parser("status")
    p_tg = sub.add_parser("telegram-setup")
    p_tg.add_argument("--test", action="store_true", help="send a test message to TELEGRAM_CHAT_ID")
    sub.add_parser("broker-check")
    args = ap.parse_args(argv)

    try:
        cfg = BotConfig.load(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd in ("run", "loop"):
        try:
            bot = _bot(cfg)
        except Exception as e:  # noqa: BLE001  missing keys etc.: clear message, no traceback
            print(f"setup error: {e}", file=sys.stderr)
            return 2

    if args.cmd == "run":
        try:
            res = bot.run_once()
        except Exception as e:  # noqa: BLE001  already alerted inside the bot
            log.error("run failed: %s", e)
            return 1
        log.info("processed %d bars, %d signals, halted=%s", res.processed_bars, len(res.signals), res.halted)
        return 0

    if args.cmd == "loop":
        while True:
            try:
                bot.run_once()
            except Exception:  # noqa: BLE001 keep the loop alive; errors are alerted inside
                log.exception("cycle failed")
            time.sleep(args.interval)

    if args.cmd == "replay":
        from .replay import replay, report
        if args.synthetic:
            from goldlab.synthetic import random_walk_bars
            raw = random_walk_bars("2024-01-01", "2024-04-01", minutes=cfg.timeframe_minutes, seed=11)
        else:
            from goldlab.data import find_data_files, load_raw
            p = Path(args.data)
            files = find_data_files(p) if p.is_dir() else [p]
            if not files:
                print(f"no CSV data in {p}; see data/README.md (or use --synthetic)", file=sys.stderr)
                return 2
            raw = load_raw(files)
            from goldlab.data import resample
            raw = resample(raw, cfg.timeframe_minutes)
        r = replay(raw, cfg, args.start, args.end)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        report(r, out)
        print(out.read_text())
        return 0 if r.match else 1

    if args.cmd == "status":
        from .state import load
        print(json.dumps(cfg.redacted(), indent=2, default=str))
        st = load(cfg.state_dir, cfg.account_balance)
        print(f"halted={st.halted} last_bar={st.engine.last_time} equity={st.engine.equity:.2f} "
              f"trades={len(st.engine.trades)} pending={len(st.engine.pending)}")
        return 0

    if args.cmd == "telegram-setup":
        from . import http
        if not cfg.telegram_bot_token:
            print("Set TELEGRAM_BOT_TOKEN first (see README step 3).", file=sys.stderr)
            return 2
        base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
        if args.test:
            if not cfg.telegram_chat_id:
                print("Set TELEGRAM_CHAT_ID too.", file=sys.stderr)
                return 2
            http.request("POST", f"{base}/sendMessage",
                         json_body={"chat_id": cfg.telegram_chat_id, "text": "goldbot test message ✅"})
            print("sent")
            return 0
        data = http.request("GET", f"{base}/getUpdates").json()
        chats = {}
        for u in data.get("result", []):
            chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
        if not chats:
            print("No messages found. Send any message (e.g. /start) to your bot in Telegram, then rerun.")
        for cid, name in chats.items():
            print(f"chat id: {cid}   ({name})")
        return 0

    if args.cmd == "broker-check":
        from .brokers import CapitalComDemo
        b = CapitalComDemo(cfg.capital_api_key, cfg.capital_identifier, cfg.capital_password, cfg.capital_epic)
        print(f"demo balance: {b.balance():,.2f}")
        print(f"{cfg.capital_epic} size rules (min, step): {b.size_rules()}")
        print(f"open positions: {b.positions()}")
        print(f"working orders: {b.working_orders()}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
