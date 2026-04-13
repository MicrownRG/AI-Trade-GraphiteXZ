"""
Chart Renderer.

Renders OHLCV candlestick charts with:
  - Fibonacci levels as horizontal lines (color-coded)
  - RSI subplot (with overbought/oversold zones)
  - Entry, SL, TP1/2/3 price markers
  - Direction-colored background shading for entry zone

Works for ALL trade types: Fibo, Reversal, Pulse.
Output is PNG bytes — ready to send via Telegram sendPhoto API.

Dependencies (add to requirements.txt):
    mplfinance>=0.12.10b0
    matplotlib>=3.8.0
"""
from __future__ import annotations

import io
from typing import Optional, TYPE_CHECKING
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from utils.logger import get_logger

if TYPE_CHECKING:
    from core.signal.fibo_engine import FiboLevels

logger = get_logger(__name__)

# ── Optional import guard ─────────────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend for server
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.lines import Line2D
    _MPL_AVAILABLE = True
except ImportError:
    _MPL_AVAILABLE = False
    logger.warning("matplotlib not installed — chart rendering disabled. Run: pip install matplotlib mplfinance")

try:
    import mplfinance as mpf
    _MPF_AVAILABLE = True
except ImportError:
    _MPF_AVAILABLE = False
    if _MPL_AVAILABLE:
        logger.warning("mplfinance not installed — Run: pip install mplfinance")


# ── Color Palette ─────────────────────────────────────────────────────────────
COLORS = {
    "entry":    "#00E5FF",   # Cyan — entry line
    "sl":       "#FF1744",   # Red  — stop loss
    "tp1":      "#76FF03",   # Light green — TP1
    "tp2":      "#00E676",   # Green       — TP2
    "tp3":      "#69F0AE",   # Mint        — TP3
    "tp_ext":   "#B9F6CA",   # Pale mint   — Extension
    "fibo_236": "#FFD54F",   # Amber
    "fibo_382": "#FFB300",   # Orange-amber
    "fibo_500": "#FF8F00",   # Deep orange
    "fibo_618": "#F57C00",   # Dark orange (key level)
    "fibo_786": "#E65100",   # Darker
    "rsi_line": "#00BCD4",   # Teal
    "rsi_ob":   "#FF5252",   # Red zone
    "rsi_os":   "#69F0AE",   # Green zone
    "bg_bull":  "#0D2B1E",   # Dark green bg for buy
    "bg_bear":  "#2B0D0D",   # Dark red bg for sell
    "candle_up": "#26A69A",  # Green candles
    "candle_dn": "#EF5350",  # Red candles
}


class ChartRenderer:
    """
    Renders trading charts as PNG bytes for Telegram.

    Usage:
        renderer = ChartRenderer()
        img_bytes = renderer.render_chart(
            df_m15=df_m15, df_m5=df_m5,
            entry_price=1980.50, stop_loss=1975.00,
            direction="buy", trade_id="FIBO-ABC123",
            fibo_levels=fibo_levels,
        )
        telegram.send_photo(img_bytes, caption="Entry Chart")
    """

    def render_chart(
        self,
        df_m15:      pd.DataFrame,
        df_m5:       pd.DataFrame,
        entry_price: float,
        stop_loss:   float,
        direction:   str,
        trade_id:    str,
        fibo_levels: Optional["FiboLevels"] = None,
        tp1:         Optional[float] = None,
        tp2:         Optional[float] = None,
        tp3:         Optional[float] = None,
        symbol:      str = "XAUUSD",
        n_candles:   int = 80,
        strategy:    str = "",
    ) -> Optional[bytes]:
        """
        Main render function. Works for all trade types.

        For Fibo trades: pass fibo_levels (all levels auto-populated).
        For others:      pass tp1, tp2, tp3 manually (or None to skip).

        Returns PNG bytes, or None if rendering fails.
        """
        if not _MPL_AVAILABLE or not _MPF_AVAILABLE:
            logger.warning("Chart rendering skipped: matplotlib/mplfinance not installed")
            return None

        try:
            return self._render(
                df_m15=df_m15, df_m5=df_m5,
                entry_price=entry_price, stop_loss=stop_loss,
                direction=direction, trade_id=trade_id,
                fibo_levels=fibo_levels,
                tp1=tp1, tp2=tp2, tp3=tp3,
                symbol=symbol, n_candles=n_candles,
                strategy=strategy,
            )
        except Exception as e:
            logger.error(f"Chart render error: {e}", exc_info=True)
            return None

    def _render(
        self,
        df_m15, df_m5,
        entry_price, stop_loss, direction, trade_id,
        fibo_levels, tp1, tp2, tp3,
        symbol, n_candles, strategy,
    ) -> bytes:
        # ── Prep data ─────────────────────────────────────────────────────────
        df = df_m15.iloc[-n_candles:].copy()
        df.index = pd.DatetimeIndex(df.index)

        # RSI from M5 (last ~4x candles for same time window)
        rsi_series = self._get_rsi(df_m5, n_candles * 4) if df_m5 is not None else None

        # ── Style ─────────────────────────────────────────────────────────────
        mc = mpf.make_marketcolors(
            up=COLORS["candle_up"], down=COLORS["candle_dn"],
            wick={"up": COLORS["candle_up"], "down": COLORS["candle_dn"]},
            edge="inherit",
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            facecolor="#0A0E1A",
            gridcolor="#1A2035",
            gridstyle="--",
            figcolor="#0A0E1A",
            y_on_right=True,
        )

        # ── Resolve levels ─────────────────────────────────────────────────────
        if fibo_levels is not None:
            _tp1 = fibo_levels.tp1
            _tp2 = fibo_levels.tp2
            _tp3 = fibo_levels.tp3
        else:
            _tp1, _tp2, _tp3 = tp1, tp2, tp3

        # ── Build addplot (RSI) ───────────────────────────────────────────────
        addplots = []
        rsi_panel_data = None

        if rsi_series is not None and len(rsi_series) >= len(df):
            # Align RSI to M15 index (downsample by taking last value per M15 candle)
            rsi_aligned = self._align_rsi_to_m15(rsi_series, df)
            if rsi_aligned is not None:
                rsi_panel_data = rsi_aligned
                rsi_ap = mpf.make_addplot(
                    rsi_aligned,
                    panel=1,
                    color=COLORS["rsi_line"],
                    linewidths=1.5,
                    ylabel="RSI",
                )
                addplots.append(rsi_ap)

        # ── Figure creation ────────────────────────────────────────────────────
        fig_kwargs = dict(
            type="candle",
            style=style,
            volume=False,
            returnfig=True,
            figsize=(14, 9) if rsi_panel_data is not None else (14, 7),
            panel_ratios=(3, 1) if rsi_panel_data is not None else (1,),
            addplot=addplots if addplots else None,
            tight_layout=True,
        )

        fig, axes = mpf.plot(df, **fig_kwargs)
        ax = axes[0]

        # ── Background shading for direction ─────────────────────────────────
        ax.set_facecolor(COLORS["bg_bull"] if direction == "buy" else COLORS["bg_bear"])

        y_min = df["low"].min()
        y_max = df["high"].max()

        def _hline(price: float, color: str, label: str, lw: float = 1.0, ls: str = "--"):
            if price is None or price <= 0:
                return
            ax.axhline(y=price, color=color, linewidth=lw, linestyle=ls, alpha=0.85)
            # Right-side label
            ax.annotate(
                f"{label}: {price:.2f}",
                xy=(1.001, (price - y_min) / (y_max - y_min)),
                xycoords="axes fraction",
                fontsize=7,
                color=color,
                va="center",
                ha="left",
            )

        # ── Draw price levels ─────────────────────────────────────────────────
        _hline(entry_price, COLORS["entry"], "ENTRY", lw=1.8, ls="-")
        _hline(stop_loss,   COLORS["sl"],    "SL",    lw=1.5)
        if _tp1: _hline(_tp1, COLORS["tp1"], "TP1")
        if _tp2: _hline(_tp2, COLORS["tp2"], "TP2", lw=1.5)
        if _tp3: _hline(_tp3, COLORS["tp3"], "TP3")

        # ── Fibo-specific overlays ────────────────────────────────────────────
        if fibo_levels is not None:
            _hline(fibo_levels.tp_extension, COLORS["tp_ext"], "EXT(161.8%)", lw=1.0, ls=":")
            _hline(fibo_levels.level_236, COLORS["fibo_236"], "23.6%", lw=0.8, ls=":")
            _hline(fibo_levels.level_382, COLORS["fibo_382"], "38.2%", lw=0.8, ls=":")
            _hline(fibo_levels.level_500, COLORS["fibo_500"], "50.0%", lw=0.8, ls=":")
            _hline(fibo_levels.level_618, COLORS["fibo_618"], "61.8% ★", lw=1.2, ls="-.")
            _hline(fibo_levels.level_786, COLORS["fibo_786"], "78.6%", lw=0.8, ls=":")

            # Entry zone shading (50%–78.6% retracement)
            zone_lo = min(fibo_levels.level_500, fibo_levels.level_786)
            zone_hi = max(fibo_levels.level_500, fibo_levels.level_786)
            ax.axhspan(zone_lo, zone_hi, alpha=0.08, color=COLORS["tp1"])

        # ── RSI panel styling ─────────────────────────────────────────────────
        if rsi_panel_data is not None and len(axes) > 1:
            rsi_ax = axes[1]
            rsi_ax.set_facecolor("#0A0E1A")
            rsi_ax.axhline(70, color=COLORS["rsi_ob"], linewidth=0.8, linestyle="--", alpha=0.7)
            rsi_ax.axhline(30, color=COLORS["rsi_os"], linewidth=0.8, linestyle="--", alpha=0.7)
            rsi_ax.axhline(50, color="#555555",        linewidth=0.6, linestyle=":")
            rsi_ax.fill_between(range(len(rsi_panel_data)), 70, 100,
                                 alpha=0.07, color=COLORS["rsi_ob"])
            rsi_ax.fill_between(range(len(rsi_panel_data)), 0, 30,
                                 alpha=0.07, color=COLORS["rsi_os"])
            rsi_ax.set_ylim(0, 100)
            rsi_ax.set_ylabel("RSI", color="#AAAAAA", fontsize=8)
            rsi_ax.tick_params(colors="#AAAAAA", labelsize=7)

            # Show current RSI value
            if fibo_levels is not None:
                rsi_ax.annotate(
                    f"RSI: {fibo_levels.rsi_value:.1f}",
                    xy=(0.02, 0.8), xycoords="axes fraction",
                    color=COLORS["rsi_line"], fontsize=8, fontweight="bold"
                )

        # ── Title ─────────────────────────────────────────────────────────────
        dir_emoji = "🟢 BUY" if direction == "buy" else "🔴 SELL"
        tier_txt  = f" [Tier {fibo_levels.signal_tier}]" if fibo_levels else ""
        strat_txt = f"  |  {strategy}" if strategy else ""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        title = f"{symbol} M15  |  {dir_emoji}{tier_txt}{strat_txt}  |  {ts}"
        ax.set_title(title, color="#FFFFFF", fontsize=9, pad=8)

        # ── Legend ────────────────────────────────────────────────────────────
        legend_elements = [
            Line2D([0], [0], color=COLORS["entry"], lw=2,  label=f"Entry {entry_price:.2f}"),
            Line2D([0], [0], color=COLORS["sl"],    lw=1.5, label=f"SL {stop_loss:.2f}"),
        ]
        if _tp1: legend_elements.append(Line2D([0],[0], color=COLORS["tp1"], lw=1, label=f"TP1 {_tp1:.2f}"))
        if _tp2: legend_elements.append(Line2D([0],[0], color=COLORS["tp2"], lw=1, label=f"TP2 {_tp2:.2f}"))
        if _tp3: legend_elements.append(Line2D([0],[0], color=COLORS["tp3"], lw=1, label=f"TP3 {_tp3:.2f}"))

        ax.legend(
            handles=legend_elements,
            loc="upper left", fontsize=7,
            facecolor="#0A0E1A", edgecolor="#333344",
            labelcolor="#CCCCCC",
        )

        # ── Save to bytes ─────────────────────────────────────────────────────
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor="#0A0E1A", edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get_rsi(df_m5: pd.DataFrame, n: int = 320) -> Optional[pd.Series]:
        """Compute RSI from M5 data."""
        if df_m5 is None or len(df_m5) < 20:
            return None
        df = df_m5.iloc[-n:].copy()
        close = df["close"]
        delta = close.diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        avg_gain = gain.ewm(com=13, min_periods=14).mean()
        avg_loss = loss.ewm(com=13, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - 100 / (1 + rs)).fillna(50.0)

    @staticmethod
    def _align_rsi_to_m15(rsi_m5: pd.Series, df_m15: pd.DataFrame) -> Optional[pd.Series]:
        """
        Downsample M5 RSI to align with M15 candle count.
        Takes every 3rd RSI value (since 1 M15 = 3 M5 candles).
        """
        try:
            # Simple approach: take every 3rd M5 RSI value and align to M15 length
            rsi_vals = rsi_m5.values
            # Resample: take end-of-period value (every 3rd)
            resampled = rsi_vals[2::3]  # index 2, 5, 8... = close of each M15 period
            n = len(df_m15)
            if len(resampled) >= n:
                resampled = resampled[-n:]
            else:
                # Pad front with 50 if not enough data
                pad = np.full(n - len(resampled), 50.0)
                resampled = np.concatenate([pad, resampled])
            return pd.Series(resampled, index=df_m15.index[:len(resampled)])
        except Exception as e:
            logger.debug(f"RSI alignment failed: {e}")
            return None
