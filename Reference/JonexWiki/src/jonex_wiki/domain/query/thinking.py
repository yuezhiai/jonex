"""Per-request presets for the two existing query-agent thinking choices."""
from jonex_wiki.kernel.errors import ValidationError


def thinking_overrides(mode: str) -> dict[str, str]:
    if mode not in ("fast", "standard", "deep"):
        raise ValidationError("thinking_mode must be fast, standard, or deep")
    return {
        "JONEX_WIKI_AGENT_THINKING": "off" if mode == "fast" else "on",
        "QUERY_SYNTHESIS_THINKING": "on" if mode == "deep" else "off",
    }
