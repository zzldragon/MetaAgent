from tool_registry import tool

import csv
import io


@tool
def load_csv(path: str, max_rows: int = 20) -> str:
    """Load a CSV file; return its columns, total row count, and the first rows.

    Args:
        path: path to the CSV file.
        max_rows: how many data rows to include in the preview.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except FileNotFoundError:
        return f"[ERROR] File not found: {path}"
    except Exception as e:
        return f"[ERROR] Could not read {path}: {e}"

    if not rows:
        return f"{path} is empty."

    header, data = rows[0], rows[1:]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(data[:max_rows])
    shown = min(max_rows, len(data))
    return (
        f"File: {path}\n"
        f"Columns ({len(header)}): {', '.join(header)}\n"
        f"Data rows: {len(data)} (showing first {shown})\n"
        f"{buf.getvalue().strip()}"
    )
