from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_as.market_meta import MarketMetaCache


class _FakeClient:
    def __init__(self, pages):
        self._pages = pages
        self.calls = 0

    def paginate(self, endpoint_path, params=None, limit=1000):  # noqa: ANN001
        self.calls += 1
        assert endpoint_path == "/markets"
        assert (params or {}).get("status") == "open"
        for p in self._pages:
            yield p, ""


def test_tau_hours_decreases_as_time_moves_forward():
    close_dt = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    c = _FakeClient(
        pages=[
            {
                "markets": [
                    {"ticker": "A", "close_time": close_dt.isoformat().replace("+00:00", "Z")},
                ]
            }
        ]
    )
    cache = MarketMetaCache(client=c, refresh_s=300.0, default_tau_hours=4.0)
    cache.refresh()
    t0 = datetime(2030, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=30)
    tau0 = cache.tau_hours_for_ticker("A", now_dt=t0)
    tau1 = cache.tau_hours_for_ticker("A", now_dt=t1)
    assert tau1 < tau0


def test_maybe_refresh_respects_interval():
    c = _FakeClient(pages=[{"markets": []}])
    cache = MarketMetaCache(client=c, refresh_s=60.0, default_tau_hours=4.0)
    assert cache.maybe_refresh(now_ts=1000.0) is True
    assert cache.maybe_refresh(now_ts=1030.0) is False
    assert cache.maybe_refresh(now_ts=1061.0) is True
    assert c.calls == 2


def test_missing_ticker_uses_default_tau():
    c = _FakeClient(pages=[{"markets": []}])
    cache = MarketMetaCache(client=c, refresh_s=60.0, default_tau_hours=3.5)
    cache.refresh()
    tau = cache.tau_hours_for_ticker("MISSING")
    assert tau == 3.5
