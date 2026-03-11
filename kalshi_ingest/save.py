from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_jsonl(path: str | Path, rows: Iterable[Dict[str, Any]]) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def try_write_csv(path: str | Path, records: List[Dict[str, Any]]) -> Optional[str]:
    try:
        import pandas as pd  # type: ignore
    except Exception as e:  # pragma: no cover
        return f"pandas not available ({e})"

    p = Path(path)
    ensure_dir(p.parent)
    df = pd.DataFrame.from_records(records)
    df.to_csv(p, index=False)
    return None


def atomic_write_text(path: str | Path, text: str) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)

