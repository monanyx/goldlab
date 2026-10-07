"""Capital.com REST API adapter — DEMO ENVIRONMENT ONLY.

Why Capital.com: official, documented REST API with a free demo account; auth
is an API key + login + API-key password; gold is a single CFD epic ("GOLD")
sized in ounces, and stop/limit levels are attached to the order itself.

The base URL is hard-coded to the demo server and there is deliberately no
setting to change it. Pointing this at a real-money account requires a code
change and review.

Implemented from Capital.com's public API reference (session/CST tokens,
/positions, /workingorders, /confirms, /accounts, /markets). It could not be
exercised against the real demo server from the build environment; the request
shapes are covered by unit tests with a mocked HTTP layer. Run
`python -m goldbot broker-check` against your demo account first.
"""

from __future__ import annotations

import math
from typing import Callable

from .. import http
from .base import Broker, BrokerError, BrokerOrder

DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"


class CapitalComDemo(Broker):
    name = "capital.com-demo"

    def __init__(self, api_key: str, identifier: str, password: str, epic: str = "GOLD",
                 req: Callable = http.request):
        if not (api_key and identifier and password):
            raise BrokerError("CAPITAL_API_KEY, CAPITAL_IDENTIFIER and CAPITAL_API_PASSWORD must be set")
        self.base = DEMO_BASE_URL
        assert "demo" in self.base, "this adapter must only ever talk to the demo environment"
        self.api_key, self.identifier, self.password, self.epic = api_key, identifier, password, epic
        self._req = req
        self._tokens: dict | None = None

    # ---- session

    def _login(self) -> None:
        r = self._req("POST", f"{self.base}/api/v1/session",
                      headers={"X-CAP-API-KEY": self.api_key},
                      json_body={"identifier": self.identifier, "password": self.password,
                                 "encryptedPassword": False})
        cst, sec = r.headers.get("CST"), r.headers.get("X-SECURITY-TOKEN")
        if not cst or not sec:
            raise BrokerError("Capital.com login did not return CST / X-SECURITY-TOKEN")
        self._tokens = {"CST": cst, "X-SECURITY-TOKEN": sec}

    def _call(self, method: str, path: str, **kw):
        if self._tokens is None:
            self._login()
        for attempt in (0, 1):
            headers = {"X-CAP-API-KEY": self.api_key, **self._tokens}
            try:
                return self._req(method, f"{self.base}{path}", headers=headers, **kw).json()
            except http.HttpError as e:
                if e.status == 401 and attempt == 0:  # session expired (10 min idle)
                    self._login()
                    continue
                raise BrokerError(str(e)) from None
        raise BrokerError("unreachable")

    # ---- account & market

    def balance(self) -> float:
        accts = self._call("GET", "/api/v1/accounts").get("accounts", [])
        if not accts:
            raise BrokerError("no accounts returned")
        acct = next((a for a in accts if a.get("preferred")), accts[0])
        return float(acct["balance"]["balance"])

    def size_rules(self) -> tuple[float, float]:
        m = self._call("GET", f"/api/v1/markets/{self.epic}")
        rules = m.get("dealingRules", {})
        min_size = float(rules.get("minDealSize", {}).get("value", 1.0))
        step = float(rules.get("minSizeIncrement", {}).get("value", min_size))
        return min_size, step

    def round_size(self, units: float) -> float:
        min_size, step = self.size_rules()
        size = math.floor(units / step + 1e-9) * step
        return round(size, 8) if size >= min_size else 0.0

    # ---- orders

    def place(self, order: BrokerOrder) -> dict:
        body = {
            "epic": self.epic,
            "direction": "BUY" if order.side == 1 else "SELL",
            "size": order.size,
            "guaranteedStop": False,
            "stopLevel": round(order.stop_level, 2),
        }
        if order.profit_level is not None:
            body["profitLevel"] = round(order.profit_level, 2)
        if order.kind == "stop":
            body.update(level=round(order.level, 2), type="STOP")
            if order.good_till is not None:
                body["goodTillDate"] = order.good_till.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S")
            path = "/api/v1/workingorders"
        else:
            path = "/api/v1/positions"
        if "stopLevel" not in body:
            raise BrokerError("refusing to send an order without a stop loss")
        ref = self._call("POST", path, json_body=body).get("dealReference")
        if not ref:
            raise BrokerError("no dealReference in response")
        conf = self._call("GET", f"/api/v1/confirms/{ref}")
        if conf.get("dealStatus") != "ACCEPTED":
            raise BrokerError(f"order rejected: {conf.get('reason') or conf}")
        deals = conf.get("affectedDeals") or []
        return {"deal_reference": ref, "deal_id": deals[0]["dealId"] if deals else conf.get("dealId"),
                "status": conf.get("status"), "request": body}

    def positions(self) -> list[dict]:
        out = []
        for p in self._call("GET", "/api/v1/positions").get("positions", []):
            pos, mkt = p.get("position", {}), p.get("market", {})
            if mkt.get("epic", self.epic) != self.epic:
                continue
            out.append({"deal_id": pos["dealId"], "side": 1 if pos["direction"] == "BUY" else -1,
                        "size": float(pos["size"]), "level": float(pos["level"]),
                        "upl": float(pos.get("upl", 0.0))})
        return out

    def working_orders(self) -> list[dict]:
        out = []
        for w in self._call("GET", "/api/v1/workingorders").get("workingOrders", []):
            d = w.get("workingOrderData", {})
            if d.get("epic", self.epic) != self.epic:
                continue
            out.append({"deal_id": d["dealId"], "side": 1 if d["direction"] == "BUY" else -1,
                        "size": float(d.get("orderSize", 0)), "level": float(d.get("orderLevel", 0))})
        return out

    def close_position(self, deal_id: str) -> None:
        self._call("DELETE", f"/api/v1/positions/{deal_id}")

    def cancel_order(self, deal_id: str) -> None:
        self._call("DELETE", f"/api/v1/workingorders/{deal_id}")
