"""Deterministic JSON serialization and SHA-256 helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import ValidationError


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically and reject non-standard numeric values."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Value is not valid canonical JSON: {exc}") from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_json(value: Any) -> str:
    return sha256_text(canonical_json(value))

