from tool_registry import tool

import pandas as pd


@tool
def csv_column_means(path: str, skip_columns: str = "") -> str:
    """Calculate the mean value of each numeric column in a CSV file.

    Args:
        path: path to the CSV file.
        skip_columns: optional comma-separated list of column names to exclude.
    """
    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        return f"[ERROR] File not found: {path}"
    except Exception as e:
        return f"[ERROR] Could not read {path}: {e}"

    # Determine which columns to skip
    skip_set = {c.strip() for c in skip_columns.split(",") if c.strip()}

    # Select only numeric columns, excluding skipped ones
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    cols_to_compute = [c for c in numeric_cols if c not in skip_set]

    if not cols_to_compute:
        return (
            f"No numeric columns found"
            + (f" (after skipping: {', '.join(sorted(skip_set))})" if skip_set else "")
            + "."
        )

    means = df[cols_to_compute].mean(numeric_only=True)

    lines = [f"Mean values for numeric columns in {path}:"]
    if skip_set:
        skipped = [c for c in numeric_cols if c in skip_set]
        if skipped:
            lines.append(f"Skipped columns: {', '.join(sorted(skipped))}")
    lines.append("")
    for col in cols_to_compute:
        lines.append(f"  {col}: {means[col]:.4f}")
    lines.append("")
    lines.append(f"Total numeric columns computed: {len(cols_to_compute)}")

    return "\n".join(lines)
