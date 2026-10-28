"""Token counting. tiktoken cl100k is a proxy for Claude's tokenizer; ratios are what matter."""

from functools import lru_cache


@lru_cache(maxsize=1)
def _enc():
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count(text: str) -> int:
    if not text:
        return 0
    enc = _enc()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text, disallowed_special=()))
