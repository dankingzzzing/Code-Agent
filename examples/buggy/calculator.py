"""Intentional defects for the homework demonstration. Do not use in production."""


def average(values):
    """Calculate an average. The exercise requires 0.0 for an empty sequence."""
    return sum(values) / len(values)


def add_tag(tag, tags=[]):
    """Add a tag. Independent calls should return independent lists."""
    tags.append(tag)
    return tags


def parse_score(raw):
    """Parse a numeric JSON value supplied by a user."""
    return eval(raw)


def load_score(raw):
    """Convert an integer string. Invalid input should raise an exception."""
    try:
        return int(raw)
    except:
        pass
