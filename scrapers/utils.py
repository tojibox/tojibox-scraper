"""
Shared utilities: fingerprinting, date conversion, local file storage.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger


def fingerprint(*parts) -> str:
    """SHA-256 of pipe-joined string parts. None values become empty string."""
    raw = "|".join(str(p) if p is not None else "" for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def ms_to_date(ms):
    """ArcGIS epoch milliseconds → ISO-8601 date string (YYYY-MM-DD)."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
    except Exception:
        return None


def ms_to_datetime(ms):
    """ArcGIS epoch milliseconds → ISO-8601 datetime string."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_raw(data: list, subdir: str, label: str, data_dir: Path):
    """
    Persist raw fetched records to data/raw/<subdir>/<label>_<ts>.json.
    Returns the file path.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = data_dir / subdir
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{label}_{ts}.json"
    with open(path, "w") as f:
        json.dump(data, f)
    logger.info(f"[{label}] Raw data saved → {path} ({len(data)} records)")
    return path


def chunk(lst: list, size: int):
    """Yield successive chunks of `size` from a list."""
    for i in range(0, len(lst), size):
        yield lst[i : i + size]
