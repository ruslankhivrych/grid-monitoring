"""Render recorded telemetry to PNG without a GUI or blocking the bot loop."""
from __future__ import annotations

import math
from datetime import datetime, timezone, tzinfo
from io import BytesIO
from threading import Lock

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import AutoDateLocator, DateFormatter
from matplotlib.figure import Figure

PERIODS = {"day": (1, "24 години"), "week": (7, "7 днів"), "month": (30, "30 днів")}
_render_lock = Lock()


def chart_series(rows: list[tuple], column: int, gap_seconds: int) -> tuple[list, list]:
    """NaNs break lines at missing readings and long measurement gaps."""
    timestamps, values = [], []
    previous = None
    for row in rows:
        ts = row[0]
        if previous is not None and ts - previous > gap_seconds:
            timestamps.append(datetime.fromtimestamp(previous + 1, tz=timezone.utc))
            values.append(math.nan)
        timestamps.append(datetime.fromtimestamp(ts, tz=timezone.utc))
        value = row[column]
        values.append(float(value) if value is not None and math.isfinite(value) else math.nan)
        previous = ts
    return timestamps, values


def render_chart(
    rows: list[tuple], start: datetime, end: datetime, zone: tzinfo,
    period: str, *, gap_seconds: int = 900,
) -> BytesIO:
    with _render_lock:
        figure = Figure(figsize=(11, 7), dpi=140, facecolor="#f4f7fb")
        FigureCanvasAgg(figure)
        battery, load = figure.subplots(2, 1, sharex=True)
        figure.subplots_adjust(left=0.10, right=0.97, bottom=0.15, top=0.82, hspace=0.35)
        figure.text(0.10, 0.95, "Батарея та навантаження", fontsize=20, weight="bold", color="#142b45")
        figure.text(
            0.10, 0.90,
            f"{PERIODS[period][1]}  ·  {start.astimezone(zone):%d.%m.%Y %H:%M} — "
            f"{end.astimezone(zone):%d.%m.%Y %H:%M}",
            fontsize=10, color="#52677c",
        )
        for axis, column, title, color in (
            (battery, 1, "Заряд батареї, %", "#2563eb"),
            (load, 2, "Навантаження, Вт", "#cf7500"),
        ):
            axis.set_facecolor("white")
            axis.set_title(title, loc="left", fontsize=12, weight="bold", color="#142b45", pad=10)
            axis.grid(axis="both", color="#e3eaf2", linewidth=0.7)
            axis.set_axisbelow(True)
            for spine in axis.spines.values():
                spine.set_color("#d5dfeb")
            axis.tick_params(colors="#52677c", labelsize=9)
            dates, values = chart_series(rows, column, gap_seconds)
            if any(math.isfinite(value) for value in values):
                axis.plot(dates, values, color=color, linewidth=1.6, marker=".", markersize=3)
            else:
                axis.text(0.5, 0.5, "Немає показників", ha="center", transform=axis.transAxes, color="#52677c")
            axis.set_xlim(start, end)
        battery.set_ylim(-3, 103)
        battery.set_yticks([0, 25, 50, 75, 100])
        load.set_ylim(bottom=0)
        locator = AutoDateLocator(tz=zone, minticks=4, maxticks=7)
        load.xaxis.set_major_locator(locator)
        load.xaxis.set_major_formatter(DateFormatter("%H:%M" if period == "day" else "%d.%m\n%H:%M", tz=zone))
        figure.text(
            0.10, 0.035,
            f"Час: {zone}  ·  Прогалини на лініях — немає свіжих даних\n"
            "Історія накопичується від моменту увімкнення цієї функції.",
            fontsize=9, color="#52677c", linespacing=1.6,
        )
        image = BytesIO()
        image.name = f"telemetry-{period}.png"
        figure.savefig(image, format="png", facecolor=figure.get_facecolor())
        figure.clear()
        image.seek(0)
        return image
