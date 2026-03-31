from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class TransitionLogger:
    """Logs market status transitions to console and a daily JSONL file.

    File writes are dispatched via ``asyncio.to_thread`` so they never
    block the event loop.
    """

    def __init__(self, log_dir: str | Path) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._path = self._log_dir / f"transitions_{date_tag}.jsonl"

    # ── Public interface ──────────────────────────────────────────────

    def log_transition(
        self,
        ticker: str,
        old_status: str,
        new_status: str,
        tracker: Any,
        metrics: dict,
    ) -> None:
        """Log a single status transition (console + disk)."""
        now = time.time()
        ts_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Console output
        label = self._transition_label(new_status)
        detail = self._detail_line(tracker, metrics, old_status, new_status)
        print(f"[{ts_str}] {label} {ticker}: {old_status} → {new_status}")
        print(f"    {detail}")

        # JSONL record
        record = {
            "ts": int(now),
            "ticker": ticker,
            "old_status": old_status,
            "new_status": new_status,
            "consecutive_passes": tracker.consecutive_passes,
            "demotion_count": tracker.demotion_count,
            "metrics": metrics,
        }
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda r=record: asyncio.ensure_future(self._write_record(r))
        )

    def log_eval_summary(self, summary: dict) -> None:
        """Print the end-of-cycle summary line."""
        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        total = summary.get("total", 0)
        watching = summary.get("WATCHING", 0)
        demoted = summary.get("DEMOTED", 0)
        blacklisted = summary.get("BLACKLISTED", 0)
        ignored = summary.get("IGNORED", 0)
        print(
            f"[{ts_str}] EVAL: {total} markets | "
            f"{watching} WATCHING | {demoted} DEMOTED | "
            f"{blacklisted} BLACKLISTED | {ignored} IGNORED"
        )

    # ── Internals ─────────────────────────────────────────────────────

    @staticmethod
    def _transition_label(new_status: str) -> str:
        labels = {
            "WATCHING": "PROMOTED",
            "DEMOTED": "DEMOTED",
            "BLACKLISTED": "BLACKLISTED",
            "IGNORED": "COOLDOWN_EXPIRED",
        }
        return labels.get(new_status, new_status)

    @staticmethod
    def _detail_line(tracker: Any, metrics: dict, old_status: str, new_status: str) -> str:
        parts = []
        spread = metrics.get("spread")
        if spread is not None:
            parts.append(f"spread={spread:.3f}")
        dv = metrics.get("dollar_volume")
        if dv is not None:
            parts.append(f"volume=${dv}")
        imb = metrics.get("imbalance")
        if imb is not None:
            parts.append(f"imbalance={imb:.2f}")
        rate = metrics.get("update_rate")
        if rate is not None:
            parts.append(f"rate={rate:.1f}/min")

        if new_status == "WATCHING":
            parts.append(f"passes={tracker.consecutive_passes}/{tracker.consecutive_passes}")
        elif new_status in ("DEMOTED", "BLACKLISTED"):
            parts.append(f"reason={tracker.last_eval_result}")
            parts.append(f"fails={tracker.consecutive_fails}/{tracker.consecutive_fails}")
            if tracker.cooldown_until:
                cd = int(tracker.cooldown_until - time.time())
                parts.append(f"cooldown={cd}s")
        return "  ".join(parts)

    async def _write_record(self, record: dict) -> None:
        try:
            await asyncio.to_thread(self._append_jsonl, record)
        except Exception:
            logger.exception("Failed to write transition record")

    def _append_jsonl(self, record: dict) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
