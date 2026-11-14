"""Writers for JSON, JSON Lines and CSV output."""

from __future__ import annotations

import csv
import json
import sys
from typing import IO, Iterable, Optional


def _open(path: Optional[str]) -> IO[str]:
    if path in (None, "-"):
        return sys.stdout
    return open(path, "w", encoding="utf-8", newline="")


def _flatten(record: dict, prefix: str = "") -> dict:
    flat = {}
    for k, v in record.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            flat.update(_flatten(v, key + "."))
        elif isinstance(v, list):
            flat[key] = json.dumps(v, ensure_ascii=False)
        else:
            flat[key] = v
    return flat


def write(records: Iterable[dict], fmt: str, path: Optional[str]) -> int:
    """Write records; returns count. JSONL streams, JSON/CSV buffer."""
    fh = _open(path)
    count = 0
    try:
        if fmt == "jsonl":
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                fh.flush()
                count += 1
        elif fmt == "json":
            rows = list(records)
            count = len(rows)
            json.dump(rows, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        elif fmt == "csv":
            rows = [_flatten(r) for r in records]
            count = len(rows)
            fields: list[str] = []
            for r in rows:
                fields.extend(k for k in r if k not in fields)
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        else:
            raise ValueError(f"Unknown format: {fmt}")
    finally:
        if fh is not sys.stdout:
            fh.close()
    return count
