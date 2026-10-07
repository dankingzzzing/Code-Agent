"""A corrected implementation of the documented demonstration contract."""

import json
import math


def average(values):
    """Return the average, or 0.0 for an empty sequence."""
    return sum(values) / len(values) if values else 0.0


def add_tag(tag, tags=None):
    """Add a tag without sharing a default list across independent calls."""
    if tags is None:
        tags = []
    tags.append(tag)
    return tags


def parse_score(raw):
    """Accept a finite numeric JSON value; reject expressions and booleans."""
    value = json.loads(raw)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("A numeric JSON value is required.")
    if not math.isfinite(value):
        raise ValueError("The score must be finite.")
    return value


def load_score(raw):
    """Let the caller observe conversion errors."""
    return int(raw)
