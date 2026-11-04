"""Retired free-text date-matrix answer shortcut.

The source-derived matrix projection remains available in the table reader.
Its dimensions are literal labels, not inferred domain aliases or proof that
any question contains no further conditions.
"""


def select_matrix_records(projection, question):
    """Defer to ordinary reading or an explicit structured query."""
    return None


def render_matrix_records(result):
    raise ValueError("UNVERIFIED_NATURAL_LANGUAGE_LOOKUP_RETIRED")
