"""General data-analysis toolset — CSV / XLSX / Parquet.

Load tabular files into a shared registry, then summarize, profile distributions,
correlate, and visualize. Format-agnostic (not domain-specific). Mirrors the
dataagent_tools idioms: one shared in-memory dataset registry with an active
pointer, @tool per top-level function, lambda-only helpers (the generator turns
every top-level `def` into a tool), and "[ERROR] ..." returns the agent recovers
from. Charts/exports save into the runtime WORKSPACE folder (so the agent's other
tools and run_python share one directory), falling back to ./data_analysis_out.

Optional engines load fail-soft: xlsx needs openpyxl, parquet needs pyarrow,
distribution fitting needs scipy — all listed in requirements.txt (the guarded
top-level imports are picked up by the codegen dependency scan) but the tools
degrade to a clear [ERROR] string if a library is genuinely absent.
"""
from tool_registry import tool

import os
import threading
import time

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")            # headless: render charts to PNG, never a window
import matplotlib.pyplot as plt

try:                             # distribution fitting + normality tests
    from scipy import stats as _stats
except ImportError:
    _stats = None
try:                             # optional pandas engines (also pulled into requirements)
    import openpyxl              # noqa: F401  — .xlsx engine
    import pyarrow               # noqa: F401  — .parquet engine
except ImportError:
    pass

# ── shared in-memory state ──────────────────────────────────────────────────
# _DATASETS is shared across all agents (tools are inlined once); each parallel
# worker should load under a UNIQUE name. The active-dataset pointer is
# THREAD-LOCAL so an orchestrator's parallel sub-agents (each its own thread)
# never clobber each other's active dataset.
_DATASETS = {}                  # name -> pandas.DataFrame
_ACTIVE = threading.local()     # _ACTIVE.name = this thread's active dataset

# Helpers are LAMBDAS on purpose (every top-level `def` is registered as a tool).
_df = lambda: _DATASETS.get(getattr(_ACTIVE, "name", None))   # this thread's active DF, or None
_pick = lambda spec, df: ([c.strip() for c in str(spec).split(",") if c.strip()]
                          if str(spec).strip() and str(spec).strip() != "all"
                          else list(df.columns))
_nums = lambda df, cols: df[cols].select_dtypes(include=[np.number])
# output dir: prefer the runtime workspace (shared with run_python), else local
_outdir = lambda: (get_workspace()[0]
                   if "get_workspace" in globals() and get_workspace()
                   else "data_analysis_out")
# Save a figure to a unique workspace path. LAMBDAS only — a top-level `def`
# here would be registered as a (junk) tool by the codegen's _tool_names scan.
_fig_path = lambda stem: os.path.abspath(os.path.join(
    _outdir(),
    "".join(c if c.isalnum() or c in "-_." else "_" for c in str(stem))[:60]
    + "_" + str(int(time.time() * 1000)) + ".png"))
_save = lambda fig, stem: (lambda p: (
    os.makedirs(os.path.dirname(p), exist_ok=True),
    fig.tight_layout(), fig.savefig(p, dpi=110), plt.close(fig), p)[-1])(_fig_path(stem))


# ── load / inspect ──────────────────────────────────────────────────────────
@tool
def load_data(path: str, name: str = "", sheet: str = "") -> str:
    """Load a CSV, XLSX/XLS or Parquet file into the dataset registry and make it
    active (format auto-detected by extension).

    Args:
        path: path to the .csv / .xlsx / .xls / .parquet file.
        name: label for the dataset (defaults to the file name).
        sheet: for Excel only — sheet name or index (default: first sheet).
    """
    try:
        if not os.path.isfile(path):
            return f"[ERROR] File not found: {path}"
        ext = os.path.splitext(path)[1].lower()
        if ext == ".csv":
            df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
        elif ext in (".xlsx", ".xls"):
            sh = (int(sheet) if str(sheet).strip().isdigit()
                  else (sheet.strip() or 0))
            df = pd.read_excel(path, sheet_name=sh)
        elif ext == ".parquet":
            df = pd.read_parquet(path)
        else:
            return (f"[ERROR] Unsupported extension '{ext}'. "
                    "Use .csv, .xlsx, .xls or .parquet.")
        label = (name or os.path.splitext(os.path.basename(path))[0]).strip()
        _DATASETS[label] = df
        _ACTIVE.name = label
        cols = ", ".join(map(str, df.columns[:25]))
        more = "" if len(df.columns) <= 25 else f" (+{len(df.columns) - 25} more)"
        return (f"Loaded '{label}' (active): {len(df)} rows x {len(df.columns)} cols.\n"
                f"Columns: {cols}{more}")
    except ImportError as e:
        return (f"[ERROR] Missing engine for {ext}: {e}. "
                "pip install openpyxl (xlsx) / pyarrow (parquet).")
    except Exception as e:
        return f"[ERROR] Could not load {path}: {e}"


@tool
def list_datasets() -> str:
    """List every loaded dataset with its shape, marking the active one."""
    if not _DATASETS:
        return "No datasets loaded. Use load_data(path=...) first."
    _act = getattr(_ACTIVE, "name", None)
    return "\n".join(
        f"- {n}{' (active)' if n == _act else ''}: "
        f"{len(d)} rows x {len(d.columns)} cols" for n, d in _DATASETS.items())


@tool
def set_active(name: str) -> str:
    """Set which loaded dataset is the active one for subsequent operations."""
    if name not in _DATASETS:
        return f"[ERROR] No dataset '{name}'. Loaded: {', '.join(_DATASETS) or '(none)'}."
    _ACTIVE.name = name
    d = _DATASETS[name]
    return f"Active dataset is now '{name}' ({len(d)} rows x {len(d.columns)} cols)."


@tool
def missing_report(columns: str = "all") -> str:
    """Per-column dtype, null count and null percentage for the active dataset."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    try:
        rows = []
        for c in _pick(columns, df):
            s = df[c]
            n = int(s.isna().sum())
            rows.append(f"  {c}: dtype={s.dtype}, nulls={n} ({100*n/max(len(s),1):.1f}%), "
                        f"unique={s.nunique()}")
        return f"Missing/dtype report ({len(df)} rows):\n" + "\n".join(rows)
    except Exception as e:
        return f"[ERROR] missing_report failed: {e}"


# ── statistics ──────────────────────────────────────────────────────────────
@tool
def describe_stats(columns: str = "all") -> str:
    """Full numeric summary of the active dataset: count, mean, median, std, var,
    min, max, SKEW (distribution bias) and KURTOSIS — per numeric column,
    optionally limited to comma-separated columns."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    try:
        num = _nums(df, _pick(columns, df))
        if num.shape[1] == 0:
            return "[ERROR] No numeric columns to summarize."
        out = pd.DataFrame({
            "count": num.count(), "mean": num.mean(), "median": num.median(),
            "std": num.std(), "var": num.var(), "min": num.min(), "max": num.max(),
            "skew(bias)": num.skew(), "kurtosis": num.kurt(),
        }).round(4)
        return out.to_string()
    except Exception as e:
        return f"[ERROR] describe_stats failed: {e}"


@tool
def quantiles(columns: str = "all", q: str = "0.25,0.5,0.75,0.9,0.95,0.99") -> str:
    """Arbitrary quantiles (comma-separated probabilities in q) of the active
    dataset's numeric columns."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    try:
        probs = [float(x) for x in str(q).split(",") if x.strip()]
        num = _nums(df, _pick(columns, df))
        if num.shape[1] == 0:
            return "[ERROR] No numeric columns."
        return num.quantile(probs).round(4).to_string()
    except Exception as e:
        return f"[ERROR] quantiles failed: {e}"


@tool
def correlation(target: str = "", method: str = "pearson") -> str:
    """Correlation of the active dataset's numeric columns. method:
    pearson|spearman|kendall. With target, returns each column's correlation with
    target sorted by |r|; otherwise the full matrix."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    if method not in ("pearson", "spearman", "kendall"):
        return "[ERROR] method must be pearson | spearman | kendall."
    try:
        num = df.select_dtypes(include=[np.number])
        if num.shape[1] < 2:
            return "[ERROR] Need at least two numeric columns."
        corr = num.corr(method=method)
        if target:
            if target not in corr.columns:
                return f"[ERROR] target '{target}' is not a numeric column."
            return corr[target].drop(target).sort_values(
                key=abs, ascending=False).round(4).to_string()
        return corr.round(3).to_string()
    except Exception as e:
        return f"[ERROR] correlation failed: {e}"


@tool
def distribution_analysis(column: str, dist: str = "norm", bins: int = 30) -> str:
    """Profile one numeric column's distribution: histogram bin counts, a fitted
    distribution (norm/lognorm/expon/gamma/...) via scipy with params + AIC, and a
    normality test (Shapiro-Wilk for n<=5000, else D'Agostino) with verdict."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    if column not in df.columns:
        return f"[ERROR] No column '{column}'."
    try:
        s = pd.to_numeric(df[column], errors="coerce").dropna()
        if len(s) < 8:
            return f"[ERROR] Need >=8 numeric values in '{column}' (got {len(s)})."
        counts, edges = np.histogram(s, bins=int(bins))
        lines = [f"Distribution of '{column}'  (n={len(s)})",
                 f"  range=[{s.min():.4g}, {s.max():.4g}]  mean={s.mean():.4g}  "
                 f"std={s.std():.4g}  skew={s.skew():.4g}  kurtosis={s.kurt():.4g}"]
        if _stats is None:
            lines.append("  (scipy not installed: fit + normality test skipped)")
            return "\n".join(lines)
        d = getattr(_stats, dist, None)
        if d is None:
            return f"[ERROR] Unknown scipy distribution '{dist}'."
        try:
            params = d.fit(s.values)
            ll = float(np.sum(d.logpdf(s.values, *params)))
            aic = 2 * len(params) - 2 * ll
            lines.append(f"  fit {dist}: params={tuple(round(float(p), 4) for p in params)}  "
                         f"loglik={ll:.1f}  AIC={aic:.1f}")
        except Exception as e:
            lines.append(f"  fit {dist} failed: {e}")
        try:
            if len(s) <= 5000:
                st, p = _stats.shapiro(s.values); test = "Shapiro-Wilk"
            else:
                st, p = _stats.normaltest(s.values); test = "D'Agostino"
            verdict = "looks normal" if p > 0.05 else "NOT normal"
            lines.append(f"  normality ({test}): stat={st:.4f}  p={p:.4g}  -> {verdict} (alpha=0.05)")
        except Exception as e:
            lines.append(f"  normality test failed: {e}")
        return "\n".join(lines)
    except Exception as e:
        return f"[ERROR] distribution_analysis failed: {e}"


# ── visualization (PNG saved to the workspace) ──────────────────────────────
@tool
def plot_histogram(column: str, bins: int = 30) -> str:
    """Histogram of a numeric column; saves a PNG to the workspace and returns its path."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    if column not in df.columns:
        return f"[ERROR] No column '{column}'."
    try:
        fig, ax = plt.subplots(figsize=(8, 5))
        pd.to_numeric(df[column], errors="coerce").dropna().plot.hist(ax=ax, bins=int(bins))
        ax.set_title(f"histogram: {column}"); ax.set_xlabel(column)
        return f"Saved histogram to {_save(fig, 'hist_' + column)}"
    except Exception as e:
        plt.close("all"); return f"[ERROR] plot_histogram failed: {e}"


@tool
def plot_box(columns: str = "all") -> str:
    """Box plot of the active dataset's numeric columns (comma-separated subset or
    'all'); saves a PNG to the workspace and returns its path."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    try:
        num = _nums(df, _pick(columns, df))
        if num.shape[1] == 0:
            return "[ERROR] No numeric columns to plot."
        fig, ax = plt.subplots(figsize=(max(6, num.shape[1] * 1.2), 5))
        num.plot.box(ax=ax); ax.set_title("box plot")
        for t in ax.get_xticklabels():
            t.set_rotation(45); t.set_ha("right")
        return f"Saved box plot to {_save(fig, 'box')}"
    except Exception as e:
        plt.close("all"); return f"[ERROR] plot_box failed: {e}"


@tool
def plot_scatter(x: str, y: str) -> str:
    """Scatter plot of column y vs column x; saves a PNG to the workspace."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    if x not in df.columns or y not in df.columns:
        return f"[ERROR] Need columns '{x}' and '{y}'."
    try:
        fig, ax = plt.subplots(figsize=(7, 6))
        ax.scatter(pd.to_numeric(df[x], errors="coerce"),
                   pd.to_numeric(df[y], errors="coerce"), s=8, alpha=0.6)
        ax.set_xlabel(x); ax.set_ylabel(y); ax.set_title(f"{y} vs {x}")
        return f"Saved scatter to {_save(fig, f'scatter_{x}_{y}')}"
    except Exception as e:
        plt.close("all"); return f"[ERROR] plot_scatter failed: {e}"


@tool
def plot_corr_heatmap(method: str = "pearson") -> str:
    """Correlation heatmap of the active dataset's numeric columns
    (pearson|spearman|kendall); saves a PNG to the workspace."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    if method not in ("pearson", "spearman", "kendall"):
        return "[ERROR] method must be pearson | spearman | kendall."
    try:
        num = df.select_dtypes(include=[np.number])
        if num.shape[1] < 2:
            return "[ERROR] Need at least two numeric columns."
        corr = num.corr(method=method)
        fig, ax = plt.subplots(figsize=(max(6, corr.shape[1] * 0.7),
                                        max(5, corr.shape[1] * 0.7)))
        im = ax.imshow(corr.values, vmin=-1, vmax=1, cmap="coolwarm")
        ax.set_xticks(range(corr.shape[1])); ax.set_yticks(range(corr.shape[1]))
        ax.set_xticklabels(corr.columns, rotation=45, ha="right", fontsize=8)
        ax.set_yticklabels(corr.columns, fontsize=8)
        fig.colorbar(im, ax=ax); ax.set_title(f"{method} correlation")
        return f"Saved correlation heatmap to {_save(fig, 'corr_heatmap')}"
    except Exception as e:
        plt.close("all"); return f"[ERROR] plot_corr_heatmap failed: {e}"


# ── export (side-effecting -> high-risk so HITL can gate it) ────────────────
@tool(risk="high")
def export_csv(path: str = "analysis_export.csv", columns: str = "all") -> str:
    """Write the active dataset (or a comma-separated subset of columns) to a CSV
    in the workspace; returns the saved path."""
    df = _df()
    if df is None:
        return "[ERROR] No active dataset. Load one with load_data(path=...)."
    try:
        d = _outdir(); os.makedirs(d, exist_ok=True)
        sub = df[_pick(columns, df)] if columns.strip() and columns != "all" else df
        fn = os.path.basename(path) or "analysis_export.csv"
        if not fn.lower().endswith(".csv"):
            fn += ".csv"
        full = os.path.abspath(os.path.join(d, fn))
        sub.to_csv(full, index=False, encoding="utf-8-sig")
        return f"Exported {len(sub)} rows x {len(sub.columns)} cols to {full}"
    except Exception as e:
        return f"[ERROR] export_csv failed: {e}"
