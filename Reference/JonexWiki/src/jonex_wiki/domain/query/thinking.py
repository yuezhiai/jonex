"""Map user-facing query modes to agent reasoning effort."""
from jonex_wiki.kernel.errors import ValidationError


def agent_effort(mode: str) -> str:
    values = {"fast": "off", "standard": "high", "deep": "xhigh"}
    if mode not in values:
        raise ValidationError("thinking_mode must be fast, standard, or deep")
    return values[mode]
