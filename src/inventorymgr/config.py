from __future__ import annotations

import csv
from pathlib import Path

from .model import ClassParams

DEFAULT_PARAMS_CSV = Path(__file__).resolve().parents[2] / "data" / "class_params.csv"


def _int_or_none(v: str | None) -> int | None:
    v = (v or "").strip()
    return int(float(v)) if v else None


def load_class_params(path: Path = DEFAULT_PARAMS_CSV) -> dict[str, ClassParams]:
    """Load per-Class lead/transit/MOQ parameters from the CSV the user maintains."""
    out: dict[str, ClassParams] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("class") or "").strip()
            if not name:
                continue
            out[name] = ClassParams(
                class_name=name,
                lead_days=_int_or_none(row.get("lead_days")),
                transit_days=_int_or_none(row.get("transit_days")),
                moq_step=_int_or_none(row.get("moq_step")),
            )
    return out
