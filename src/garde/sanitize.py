def clean_text(value: object, limit: int = 40) -> str:
    """Single-line, printable, length-limited text. Strips control and invisible characters."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def clean_optional(value: object, limit: int = 40) -> str | None:
    if value is None:
        return None
    return clean_text(value, limit) or None


def short_address(address: str) -> str:
    cleaned = clean_text(address, 80)
    return cleaned if len(cleaned) <= 12 else f"{cleaned[:6]}…{cleaned[-4:]}"


def safe_int(value: object, *, minimum: int = 0, maximum: int | None = None) -> int | None:
    """Parse API numbers (usually decimal strings). Returns None for anything odd."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    if isinstance(value, str) and not (value.isascii() and value.isdigit()):
        return None
    try:
        number = int(value)
    except ValueError:  # e.g. absurdly long digit strings
        return None
    if number < minimum or (maximum is not None and number > maximum):
        return None
    return number