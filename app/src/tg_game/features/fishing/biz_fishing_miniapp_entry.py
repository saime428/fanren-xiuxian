import re
from urllib.parse import parse_qsl, urlsplit


_URL_PATTERN = re.compile(r"(?:https?|tg)://[^\s<>'\"）)]+", re.IGNORECASE)
_FISH_TOKEN_PATTERN = re.compile(r"^(?:fish_)?[A-Za-z0-9_-]{4,160}$", re.IGNORECASE)


def _safe_text(value: object, max_length: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = _URL_PATTERN.sub("[url]", text)
    if len(text) > max_length:
        return f"{text[: max_length - 1]}..."
    return text


def _parse_pairs(text: str) -> list[tuple[str, str]]:
    if not text or "=" not in text:
        return []
    try:
        return [(str(key), str(value)) for key, value in parse_qsl(text, keep_blank_values=True)]
    except ValueError:
        return []


def _start_param_kind(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return ""
    if lowered.startswith("fishing"):
        return "fishing"
    if lowered.startswith("fish"):
        return "fish"
    match = re.match(r"([a-z][a-z0-9]{1,15})(?:[_:\-.]|$)", lowered)
    return match.group(1) if match else "present"


def _host_from_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    return (parsed.hostname or parsed.netloc or "").lower()


def _origin_from_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"
