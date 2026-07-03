from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .config import FEATURE_COLUMNS


BASE_AUDIT_COLUMNS = [
    "date",
    "label_ce",
    "label_pe",
    "ce_eligible",
    "pe_eligible",
    "supertrend_dir",
    "price_vs_vwap",
    "adx",
    "di_spread",
    "volatility",
    "mins_since_open",
    "mins_to_close",
    "time_to_expiry_min",
    "momentum_velocity",
    "range_compression",
    "returns",
    "return_3",
    "atr",
    "moneyness",
    "hour",
    "weekday",
]


@dataclass
class LabelAudit:
    rows: int
    start: str
    end: str
    ce_positive_rate: float
    pe_positive_rate: float
    both_positive_rate: float
    neither_positive_rate: float
    ce_eligible_rate: float
    pe_eligible_rate: float


def read_audit_dataset(path: Path, extra_columns: Iterable[str] = ()) -> pd.DataFrame:
    wanted = set(BASE_AUDIT_COLUMNS) | set(extra_columns)
    df = pd.read_csv(path, usecols=lambda col: col in wanted)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for col in df.columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def label_audit(df: pd.DataFrame) -> LabelAudit:
    ce = df["label_ce"].astype(int)
    pe = df["label_pe"].astype(int)
    return LabelAudit(
        rows=int(len(df)),
        start=str(df["date"].min()),
        end=str(df["date"].max()),
        ce_positive_rate=float(ce.mean()),
        pe_positive_rate=float(pe.mean()),
        both_positive_rate=float(((ce == 1) & (pe == 1)).mean()),
        neither_positive_rate=float(((ce == 0) & (pe == 0)).mean()),
        ce_eligible_rate=float(df.get("ce_eligible", pd.Series(dtype=float)).mean()),
        pe_eligible_rate=float(df.get("pe_eligible", pd.Series(dtype=float)).mean()),
    )


def bucket_rates(df: pd.DataFrame) -> dict:
    out: dict[str, dict] = {}
    out["by_year"] = (
        df.assign(year=df["date"].dt.year)
        .groupby("year")[["label_ce", "label_pe"]]
        .agg(["count", "mean"])
        .pipe(_flatten_columns)
        .to_dict(orient="index")
    )
    out["by_hour"] = (
        df.groupby("hour")[["label_ce", "label_pe"]]
        .agg(["count", "mean"])
        .pipe(_flatten_columns)
        .to_dict(orient="index")
    )

    tod_bins = [0, 30, 60, 90, 120, 180, 240, 300, 360, 376]
    tod_labels = [
        "open_0_30",
        "morning_30_60",
        "morn_60_90",
        "morn_90_120",
        "mid_120_180",
        "mid_180_240",
        "aft_240_300",
        "close_300_360",
        "last_360_375",
    ]
    temp = df.copy()
    temp["tod_bucket"] = pd.cut(
        temp["mins_since_open"], bins=tod_bins, labels=tod_labels, right=False
    )
    out["by_time_bucket"] = (
        temp.groupby("tod_bucket", observed=True)[["label_ce", "label_pe"]]
        .agg(["count", "mean"])
        .pipe(_flatten_columns)
        .to_dict(orient="index")
    )

    vol90 = float(temp["volatility"].quantile(0.90))
    conditions = [
        (temp["volatility"] >= vol90) | (temp["adx"] >= 35),
        (temp["adx"] >= 25) & (temp["di_spread"].abs() >= 10),
        temp["adx"] < 18,
    ]
    choices = ["volatile_trend", "trend", "range"]
    temp["regime_proxy"] = np.select(conditions, choices, default="mixed")
    out["by_regime_proxy"] = (
        temp.groupby("regime_proxy")[["label_ce", "label_pe"]]
        .agg(["count", "mean"])
        .pipe(_flatten_columns)
        .to_dict(orient="index")
    )
    return out


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [
        "_".join(str(part) for part in col if str(part))
        if isinstance(col, tuple)
        else str(col)
        for col in df.columns
    ]
    return df


def feature_ranges(df: pd.DataFrame) -> dict[str, dict[str, float]]:
    cols = [
        "momentum_velocity",
        "range_compression",
        "price_vs_vwap",
        "adx",
        "di_spread",
        "returns",
        "return_3",
        "volatility",
        "atr",
        "time_to_expiry_min",
        "moneyness",
    ]
    out = {}
    for col in cols:
        if col not in df:
            continue
        s = df[col].dropna()
        out[col] = {
            "min": float(s.min()),
            "p01": float(s.quantile(0.01)),
            "p50": float(s.quantile(0.50)),
            "p99": float(s.quantile(0.99)),
            "max": float(s.max()),
        }
    return out


def static_feature_parity_warnings() -> list[str]:
    return [
        "momentum_velocity currently differs between training and live: training uses diff(returns), live uses point acceleration.",
        "time_to_expiry_min currently equals minutes to market close, not true contract time to expiry.",
        "volume_ratio is always 1.0 for zero-volume index history; live option liquidity needs separate features.",
    ]


def write_markdown_report(path: Path, audit: LabelAudit, buckets: dict, ranges: dict) -> None:
    lines = [
        "# Pipeline V2 Phase 1 Audit Output",
        "",
        "## Label Summary",
        "",
        f"- Rows: {audit.rows:,}",
        f"- Date range: {audit.start} to {audit.end}",
        f"- CE positive rate: {audit.ce_positive_rate:.6f}",
        f"- PE positive rate: {audit.pe_positive_rate:.6f}",
        f"- Both positive rate: {audit.both_positive_rate:.6f}",
        f"- Neither positive rate: {audit.neither_positive_rate:.6f}",
        f"- CE eligible rate: {audit.ce_eligible_rate:.6f}",
        f"- PE eligible rate: {audit.pe_eligible_rate:.6f}",
        "",
        "## Static Feature Parity Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in static_feature_parity_warnings())
    lines.extend(["", "## Feature Ranges", ""])
    for name, stats in ranges.items():
        lines.append(
            f"- {name}: min={stats['min']:.8g}, p01={stats['p01']:.8g}, "
            f"p50={stats['p50']:.8g}, p99={stats['p99']:.8g}, max={stats['max']:.8g}"
        )
    lines.extend(
        [
            "",
            "## Bucket Data",
            "",
            "Detailed bucket data is written to the companion JSON report.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def to_jsonable(audit: LabelAudit, buckets: dict, ranges: dict) -> dict:
    return {
        "label_audit": asdict(audit),
        "bucket_rates": buckets,
        "feature_ranges": ranges,
        "feature_parity_warnings": static_feature_parity_warnings(),
        "feature_columns": FEATURE_COLUMNS,
    }
