import os

from tool_registry import tool


@tool
def write_human_words_to_file(filename: str, content: str) -> str:
    """Write text content into a file under the workspace directory.

    The file is saved in the current working directory (workspace).

    Args:
        filename: The name of the file to write (e.g., 'notes.txt').
        content: The text content to write into the file.
    """
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        full_path = os.path.abspath(filename)
        return f"Successfully wrote {len(content)} characters to {full_path}"
    except Exception as e:
        return f"[ERROR] Failed to write file: {e}"
