from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from .stream import run_ws_stream


def main() -> None:
    load_dotenv(override=False)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    base_url = os.getenv("KALSHI_WS_URL", "")
    out_dir = os.getenv("KALSHI_WS_OUT_DIR", "data/kalshi/ws")
    trade_buffer_size = int(os.getenv("KALSHI_WS_TRADE_BUFFER", "5000"))

    asyncio.run(
        run_ws_stream(
            base_url=base_url or None,
            out_dir=out_dir,
            trade_buffer_size=trade_buffer_size,
        )
    )


if __name__ == "__main__":
    main()
