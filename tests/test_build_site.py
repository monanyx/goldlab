"""The results page is built only from research outputs and stays self-contained."""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_site  # noqa: E402


def _stats(r, n=50, ret=0.05):
    return {"trades": n, "expectancy_r": r, "total_return": ret, "r_t_stat": 1.0}


def _results(tmp_path: Path) -> Path:
    res = tmp_path / "results"
    res.mkdir()
    summary = {
        "london_breakout": {"verdict": "INCONCLUSIVE", "stats": {"Full (default)": _stats(0.07)},
                            "holdout": {"Holdout (default)": _stats(-0.10, ret=-0.25)}, "by_tag": []},
        "regime_switch": {"verdict": "NO EDGE", "stats": {"Full (default)": _stats(-0.25)},
                          "holdout": {"Holdout (default)": _stats(-0.09)},
                          "adoption": {"adopted": False, "checks": {"a": True, "b": False}},
                          "by_tag": [{"tag": "range_fade_long", "trades": 25, "win_rate": 0.16,
                                      "expectancy_r": -0.48},
                                     {"tag": "trend_long_break", "trades": 38, "win_rate": 0.47,
                                      "expectancy_r": -0.04}]},
        "<script>alert(1)</script>": {"verdict": "NO EDGE", "stats": {"Full (default)": _stats(0.01)},
                                      "holdout": {"Holdout (default)": _stats(0.02)}, "by_tag": []},
    }
    (res / "summary.json").write_text(json.dumps(summary))
    (res / "data_quality.json").write_text(json.dumps(
        {"rows_after_clean": 96748, "first_bar": "2022-10-30 22:00", "last_bar": "2026-11-13 00:00"}))
    (res / "replay_check.md").write_text("- Trades: bot 49, backtest 49\n- **Result: MATCH — identical trades**")
    for period, r in (("holdout", [-1.0, 0.5, -1.0]), ("default", [1.5, -1.0, 1.5])):
        days = pd.date_range("2023-01-02" if period == "holdout" else "2025-01-02", periods=3, freq="D", tz="UTC")
        pd.DataFrame({"exit_time": days, "r_multiple": r}).to_csv(res / f"trades_london_breakout_{period}.csv",
                                                                   index=False)
    return res


def test_page_is_built_from_results_and_self_contained(tmp_path):
    page = build_site.build(build_site.load(_results(tmp_path)), "someone/goldlab", tests=125)
    assert "{{" not in page
    assert "2 of 3" not in page and "1 of 3" in page  # only the escaped strategy's holdout is positive
    assert "49/49" in page and "96,748" in page and ">125<" in page
    assert "https://github.com/someone/goldlab" in page
    # nothing is loaded from elsewhere
    assert "<script src" not in page and "<link" not in page and "@import" not in page
    # names from the data are escaped, never injected as markup
    assert "<script>alert(1)</script>" not in page
    # the cumulative curve chains the holdout into the development window
    data = json.loads(page.split('id="page-data">')[1].split("</script>")[0].replace("<\\/", "</"))
    assert [p["c"] for p in data["curve"]] == [-1.0, -0.5, -1.5, 0.0, -1.0, 0.5]
    assert data["holdEnd"] == "2023-01-04"
    assert "not adopted" in page and "Range day, fade (long)" in page


def test_page_without_holdout_or_test_count(tmp_path):
    res = _results(tmp_path)
    s = json.loads((res / "summary.json").read_text())
    for v in s.values():
        v["holdout"] = None
    (res / "summary.json").write_text(json.dumps(s))
    page = build_site.build(build_site.load(res), "someone/goldlab", tests=None)
    assert "Automated tests" not in page and "{{" not in page
