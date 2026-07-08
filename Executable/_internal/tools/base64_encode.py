import base64

from tool_registry import tool


@tool
def base64_encode(sentence: str) -> str:
    """Encode a sentence into base64 string.

    Args:
        sentence: The plain text sentence to encode.
    """
    try:
        encoded = base64.b64encode(sentence.encode("utf-8")).decode("utf-8")
        return encoded
    except Exception as e:
        return f"[ERROR] Failed to encode: {e}"
