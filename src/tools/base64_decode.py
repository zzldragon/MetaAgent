import base64

from tool_registry import tool


@tool
def base64_decode(encoded_string: str) -> str:
    """Decode a base64-encoded string back to plain text.

    Args:
        encoded_string: The base64 encoded string to decode.
    """
    try:
        decoded = base64.b64decode(encoded_string.encode("utf-8")).decode("utf-8")
        return decoded
    except Exception as e:
        return f"[ERROR] Failed to decode: {e}"
