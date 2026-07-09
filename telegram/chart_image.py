import os
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


_BG = "#ffffff"
_GRID = "#e8edf3"
_TEXT = "#1f2937"
_UP = "#138f5a"
_DOWN = "#d94b4b"
_PRICE = "#2563eb"


def _price_fmt(value: float) -> str:
    return f"{float(value):,.1f}"


def render_banknifty_chart(chart: dict, out_dir: str) -> str | None:
    bars = chart.get("bars") or []
    if len(bars) < 2:
        return None

    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"banknifty_5m_{ts}.png")

    opens = [float(b["open"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    labels = [str(b.get("label") or b.get("ts") or "") for b in bars]

    fig, ax = plt.subplots(figsize=(10.5, 5.6), dpi=160)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    x_vals = list(range(len(bars)))
    price_range = max(highs) - min(lows)
    min_body = max(price_range * 0.01, 0.8)

    for idx, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
        color = _UP if c >= o else _DOWN
        ax.vlines(idx, l, h, color=color, linewidth=1.4, alpha=0.95, zorder=2)

        body_low = min(o, c)
        body_high = max(o, c)
        body_height = max(body_high - body_low, min_body)
        if body_high - body_low < min_body:
            body_low = ((o + c) / 2.0) - (body_height / 2.0)

        ax.add_patch(
            Rectangle(
                (idx - 0.32, body_low),
                0.64,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=1.0,
                alpha=0.92,
                zorder=3,
            )
        )

    last_price = closes[-1]
    first_price = opens[0]
    change = last_price - first_price
    change_pct = (change / first_price * 100.0) if first_price else 0.0

    ax.axhline(last_price, color=_PRICE, linestyle="--", linewidth=1.0, alpha=0.7)
    ax.text(
        len(bars) - 0.05,
        last_price,
        f"  {_price_fmt(last_price)}",
        color=_PRICE,
        fontsize=9,
        va="center",
        ha="left",
    )

    ax.set_title(
        f"BANKNIFTY 5-Min Candles   {_price_fmt(last_price)}   ({change:+.1f}, {change_pct:+.2f}%)",
        loc="left",
        fontsize=13,
        color=_TEXT,
        pad=14,
        fontweight="bold",
    )
    ax.text(
        0.0,
        1.01,
        f"{labels[0]} -> {labels[-1]}   H {_price_fmt(max(highs))}   L {_price_fmt(min(lows))}",
        transform=ax.transAxes,
        fontsize=9,
        color="#4b5563",
        ha="left",
        va="bottom",
    )

    tick_step = 1 if len(labels) <= 8 else 2 if len(labels) <= 14 else 3
    tick_idx = x_vals[::tick_step]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([labels[i] for i in tick_idx], fontsize=9, color="#6b7280")
    ax.tick_params(axis="y", labelsize=9, colors="#6b7280")

    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color("#d1d5db")
    ax.spines["bottom"].set_color("#d1d5db")
    ax.grid(True, axis="y", color=_GRID, linewidth=0.9)
    ax.grid(False, axis="x")

    ax.set_xlim(-0.8, len(bars) - 0.1)
    pad = max(price_range * 0.12, 8.0)
    ax.set_ylim(min(lows) - pad, max(highs) + pad)

    plt.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path
