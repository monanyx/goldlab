"""Bot configuration.

Non-secret settings live in a TOML file (default `goldbot.toml`). Secrets are
read ONLY from environment variables and are never written anywhere.
A few non-secret settings can be overridden by environment variables so that
GitHub Actions variables can flip them without a commit.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from goldlab.config import CostConfig, RiskConfig

MODES = ("signal", "paper", "live")
LIVE_CONFIRM_ENV = "GOLDBOT_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_UNDERSTAND_THIS_PLACES_ORDERS_ON_DEMO"

SECRET_ENV = {
    "twelvedata_api_key": "TWELVEDATA_API_KEY",
    "alphavantage_api_key": "ALPHAVANTAGE_API_KEY",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "capital_api_key": "CAPITAL_API_KEY",
    "capital_identifier": "CAPITAL_IDENTIFIER",
    "capital_password": "CAPITAL_API_PASSWORD",
}


class ConfigError(ValueError):
    pass


def _truthy(v: str | None) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class BotConfig:
    # --- mode & safety
    mode: str = "signal"
    live_trading_enabled: bool = False  # live also needs GOLDBOT_LIVE_CONFIRM in the environment
    kill_switch: bool = False  # true = halt everything

    # --- strategy
    strategy: str = "auto"  # auto | london_breakout | ny_momentum | vwap_fade | "pkg.module:Class"
    strategy_params: dict = field(default_factory=dict)
    report_summary: str = "results/summary.json"  # read by strategy = "auto"

    # --- account & hard risk rules
    account_balance: float = 10_000.0
    risk_pct: float = 0.01
    daily_loss_limit_pct: float = 0.03
    max_trades_per_day: int = 3
    no_new_trades_after: str = "20:00"
    timezone: str = "Europe/Vienna"

    # --- costs used for paper fills and sizing (USD per oz)
    spread: float = 0.35
    slippage: float = 0.05
    min_units: float = 1.0  # smallest tradeable size in oz (0.01 lot)
    unit_step: float = 1.0

    # --- data
    data_provider: str = "twelvedata"  # twelvedata | alphavantage | csv
    symbol: str = "XAU/USD"
    timeframe_minutes: int = 15
    history_bars: int = 3000
    csv_path: str = "data"

    # --- alerts & state
    daily_summary_time: str = "22:30"  # local time (see `timezone`)
    state_dir: str = "state"
    capital_epic: str = "GOLD"

    # --- secrets (env only)
    twelvedata_api_key: str = field(default="", repr=False)
    alphavantage_api_key: str = field(default="", repr=False)
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    capital_api_key: str = field(default="", repr=False)
    capital_identifier: str = field(default="", repr=False)
    capital_password: str = field(default="", repr=False)

    # ------------------------------------------------------------------ build

    @classmethod
    def load(cls, path: str | Path | None = None, env: dict | None = None) -> "BotConfig":
        env = dict(os.environ if env is None else env)
        data: dict = {}
        if path is not None and Path(path).exists():
            data = tomllib.loads(Path(path).read_text())
        known = {f.name for f in fields(cls)}
        for k in data:
            if k in SECRET_ENV:
                raise ConfigError(f"'{k}' is a secret: set it with the {SECRET_ENV[k]} environment variable, "
                                  "not in the config file")
            if k not in known:
                raise ConfigError(f"unknown config key '{k}'")
        cfg = cls(**data)
        # non-secret env overrides (handy for GitHub Actions variables)
        if env.get("GOLDBOT_MODE"):
            cfg.mode = env["GOLDBOT_MODE"].strip().lower()
        if _truthy(env.get("GOLDBOT_KILL_SWITCH")):
            cfg.kill_switch = True
        if env.get("GOLDBOT_ACCOUNT_BALANCE"):
            cfg.account_balance = float(env["GOLDBOT_ACCOUNT_BALANCE"])
        for attr, var in SECRET_ENV.items():
            setattr(cfg, attr, env.get(var, "").strip())
        cfg._live_confirm = env.get(LIVE_CONFIRM_ENV, "")
        cfg._forbid_live = _truthy(env.get("GOLDBOT_FORBID_LIVE"))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ConfigError(f"mode must be one of {MODES}, got '{self.mode}'")
        if not 0 < self.risk_pct <= 0.02:
            raise ConfigError("risk_pct must be in (0, 0.02]")
        if not 0 < self.daily_loss_limit_pct <= 0.03:
            raise ConfigError("daily_loss_limit_pct must be in (0, 0.03]: the 3% daily limit is a hard rule")
        if not 1 <= self.max_trades_per_day <= 3:
            raise ConfigError("max_trades_per_day must be 1..3: max 3 trades per day is a hard rule")
        hh, mm = map(int, self.no_new_trades_after.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ConfigError("no_new_trades_after must be HH:MM")
        if self.mode == "live":
            if getattr(self, "_forbid_live", False):
                raise ConfigError("live mode is forbidden in this environment (GOLDBOT_FORBID_LIVE is set)")
            if not self.live_trading_enabled:
                raise ConfigError("mode = 'live' requires live_trading_enabled = true in the config file")
            if getattr(self, "_live_confirm", "") != LIVE_CONFIRM_VALUE:
                raise ConfigError(f"mode = 'live' requires the environment variable "
                                  f"{LIVE_CONFIRM_ENV}={LIVE_CONFIRM_VALUE}")

    # ------------------------------------------------------------------ derived

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def risk_config(self, equity: float | None = None) -> RiskConfig:
        return RiskConfig(
            risk_pct=self.risk_pct, daily_loss_limit_pct=self.daily_loss_limit_pct,
            max_trades_per_day=self.max_trades_per_day,
            initial_equity=self.account_balance if equity is None else equity,
            min_units=self.min_units, unit_step=self.unit_step,
            no_entry_after=self.no_new_trades_after, no_entry_tz=self.timezone,
        )

    def cost_config(self) -> CostConfig:
        return CostConfig(spread=self.spread, slippage=self.slippage)

    def redacted(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = ("set" if v else "missing") if f.name in SECRET_ENV else v
        return out
