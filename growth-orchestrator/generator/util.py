"""Small deterministic helpers. Never use Python's built-in hash(): it is salted per process."""
from __future__ import annotations

import hashlib
import json
import random
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def rng(seed: int, stage: str) -> random.Random:
    """Independent RNG per stage/entity so changing one stage never shifts the others."""
    return random.Random(f"{seed}:{stage}")


def sha(*parts, n: int = 12) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:n]


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(s).lower())


def wpick(r: random.Random, pairs):
    """Weighted pick from [(item, weight), ...]."""
    total = sum(w for _, w in pairs)
    x = r.random() * total
    acc = 0.0
    for item, w in pairs:
        acc += w
        if x < acc:
            return item
    return pairs[-1][0]


def quota_assign(r: random.Random, n: int, quotas: dict[str, float]) -> list[str]:
    """Exact quotas (largest remainder) in shuffled order, so rare cases always get coverage."""
    total = sum(quotas.values())
    raw = {k: n * v / total for k, v in quotas.items()}
    counts = {k: int(v) for k, v in raw.items()}
    rest = n - sum(counts.values())
    order = sorted(raw.items(), key=lambda kv: (kv[1] - int(kv[1]), kv[0]), reverse=True)
    for k, _ in order[:rest]:
        counts[k] += 1
    out = [k for k, c in counts.items() for _ in range(c)]
    r.shuffle(out)
    return out


def write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
