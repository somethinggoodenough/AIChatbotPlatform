"""A bounded normalization pass, not a universal injection detector."""

import base64
import binascii
import html
import re
import unicodedata
from urllib.parse import unquote


# Common Latin-lookalike Cyrillic/Greek characters in the test corpus.
HOMOGLYPHS = str.maketrans("аесорхуіΑΒΕΙΚΜΝΟΡΤΧ", "aecopxyiABEIKMNOPTX")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(HOMOGLYPHS)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    for _ in range(2):
        text = html.unescape(unquote(text))

    def decode(match):
        try:
            return base64.b64decode(match[1], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return match[0]

    return re.sub(r"base64:([A-Za-z0-9+/=]+)", decode, text, flags=re.IGNORECASE)


def looks_risky(text: str) -> bool:
    value = normalize(text).lower()
    return any(marker in value for marker in (
        "ignore previous", "ignore all", "system prompt", "<system>",
        "developer message", "reveal secret", "override instructions",
    ))


def untrusted_data(label: str, value: str) -> str:
    """JSON quoting keeps a tool result from closing our data delimiter."""
    import json
    return f"UNTRUSTED_DATA {label}\n{json.dumps(value, ensure_ascii=False)}\nEND_UNTRUSTED_DATA"
