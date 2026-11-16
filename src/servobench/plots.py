"""Plots of a run, saved as PNG files.

Drawn with matplotlib's ``Figure`` and its Agg canvas directly, never with
``pyplot``, so plotting needs no display and changes no global state.

``plot_run``: commanded and measured position against time, and below it, on
its own axis, the temperature. Limits that fall inside the plotted range are
drawn as dashed lines; the moment a run stopped early is marked on both panels.
"""

from __future__ import annotations

import math
from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from servobench.contract import POSITION, TEMPERATURE
from servobench.limits import Limits

# Colours: two categorical slots, checked for colour-vision deficiency, then ink
# and chrome. The abort marker is the "critical" status colour and is always
# labelled in text.
COMMANDED = "#2a78d6"
MEASURED = "#eb6834"
INK = "#0b0b0b"
SECONDARY_INK = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"
CRITICAL = "#d03b3b"


def plot_run(
    trace, limits: Limits, path: str | Path, *, title: str, measured_label: str = "measured"
) -> Path:
    """Save a plot of ``trace`` (a ``runner.Trace``) to ``path`` as PNG; return the path.

    ``measured_label`` names the position readings in the legend; for a
    rehearsal they are the simulated servo's, not a measurement.
    """
    path = Path(path)
    origin_s = trace.t_start_s if trace.t_start_s is not None else trace.samples[0].t_s

    figure = Figure(figsize=(10.0, 6.5), dpi=120, facecolor=SURFACE, layout="constrained")
    FigureCanvasAgg(figure)
    position_axes, temperature_axes = figure.subplots(
        2, 1, sharex=True, gridspec_kw={"height_ratios": (3, 1.4)}
    )
    for axes in (position_axes, temperature_axes):
        _style(axes)

    times = [sample.t_s - origin_s for sample in trace.samples]
    measured_deg = [math.degrees(sample.values.get(POSITION, math.nan)) for sample in trace.samples]
    end_s = times[-1] if times else 0.0

    # The command: held from enable, then each command until the next.
    command_times, command_deg = [], []
    if trace.t_enable_s is not None and trace.enable_position_rad is not None:
        command_times.append(trace.t_enable_s - origin_s)
        command_deg.append(math.degrees(trace.enable_position_rad))
    for command in trace.commands:
        if command.name == POSITION:
            command_times.append(command.t_s - origin_s)
            command_deg.append(math.degrees(command.value))
    if command_times:
        command_times.append(max(end_s, command_times[-1]))
        command_deg.append(command_deg[-1])
        position_axes.step(
            command_times, command_deg, where="post", color=COMMANDED, linewidth=1.6,
            label="commanded", zorder=3,
        )  # fmt: skip
    position_axes.plot(times, measured_deg, color=MEASURED, linewidth=1.4, label=measured_label)
    position_axes.set_ylabel("position (deg)", color=SECONDARY_INK)
    _limit_lines(
        position_axes,
        command_deg + measured_deg,
        (
            (math.degrees(limits.position_min_rad), "position_min"),
            (math.degrees(limits.position_max_rad), "position_max"),
        ),
    )
    legend = position_axes.legend(loc="upper left", frameon=False, fontsize=9)
    for text in legend.get_texts():
        text.set_color(SECONDARY_INK)

    temperatures = [sample.values.get(TEMPERATURE, math.nan) for sample in trace.samples]
    temperature_axes.plot(times, temperatures, color=SECONDARY_INK, linewidth=1.4)
    temperature_axes.set_ylabel("temperature (degC)", color=SECONDARY_INK)
    temperature_axes.set_xlabel(
        "time since the experiment started (s)" if trace.t_start_s is not None else "time (s)",
        color=SECONDARY_INK,
    )
    _limit_lines(
        temperature_axes,
        temperatures,
        (
            (limits.temperature_max_degc, "temperature_max"),
            (limits.start_temperature_max_degc, "start_temperature_max"),
        ),
    )

    stop = trace.stop
    if stop is not None and stop.t_s is not None:
        stop_s = stop.t_s - origin_s
        for axes in (position_axes, temperature_axes):
            axes.axvline(stop_s, color=CRITICAL, linewidth=1.2)
        position_axes.annotate(
            f"stopped: {stop.kind}",
            xy=(stop_s, 1.0),
            xycoords=("data", "axes fraction"),
            xytext=(-4, -4),
            textcoords="offset points",
            ha="right",
            va="top",
            fontsize=9,
            color=INK,
        )

    figure.suptitle(title, color=INK, fontsize=11, x=0.01, ha="left")
    figure.savefig(path, format="png", facecolor=SURFACE)
    return path


def plot_overlay(
    path: str | Path,
    *,
    title: str,
    times_s,
    measured_rad,
    default_rad,
    fitted_rad,
    command_times_s,
    command_rad,
    default_error_rad: float,
    fitted_error_rad: float,
    noise_floor_rad: float,
    fit_quality: str,
) -> Path:
    """The held-out experiment: measured position, the default model and the fitted model.

    Above, the three positions and the command against time; below, each
    model's error (model minus measured) with the readings' noise floor as a
    band. Each model's RMS error is in its legend label and in the title
    block, in degrees.
    """
    path = Path(path)
    figure = Figure(figsize=(10.0, 7.0), dpi=120, facecolor=SURFACE, layout="constrained")
    FigureCanvasAgg(figure)
    top, bottom = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": (3, 1.6)})
    for axes in (top, bottom):
        _style(axes)
    origin = command_times_s[0] if len(command_times_s) else times_s[0]
    t = [value - origin for value in times_s]
    deg = math.degrees
    if len(command_times_s):
        ct = [value - origin for value in command_times_s] + [t[-1]]
        cd = [deg(value) for value in command_rad] + [deg(command_rad[-1])]
        top.step(ct, cd, where="post", color=MUTED, linewidth=1.0, label="commanded", zorder=1)
    top.plot(t, [deg(v) for v in measured_rad], color=INK, linewidth=1.6, label="measured",
             zorder=2)  # fmt: skip
    top.plot(
        t, [deg(v) for v in default_rad], color=MEASURED, linewidth=1.2, linestyle=(0, (4, 2)),
        label=f"default model: RMS error {deg(default_error_rad):.3f} deg", zorder=3,
    )  # fmt: skip
    top.plot(
        t, [deg(v) for v in fitted_rad], color=COMMANDED, linewidth=1.2,
        label=f"fitted model: RMS error {deg(fitted_error_rad):.3f} deg", zorder=4,
    )  # fmt: skip
    top.set_ylabel("position (deg)", color=SECONDARY_INK)
    legend = top.legend(loc="upper right", frameon=False, fontsize=9)
    for text in legend.get_texts():
        text.set_color(SECONDARY_INK)

    floor = deg(noise_floor_rad)
    bottom.axhspan(-floor, floor, color=GRID, zorder=0)
    bottom.plot(t, [deg(m - x) for m, x in zip(default_rad, measured_rad, strict=True)],
                color=MEASURED, linewidth=1.0, label="default model - measured")  # fmt: skip
    bottom.plot(t, [deg(f - x) for f, x in zip(fitted_rad, measured_rad, strict=True)],
                color=COMMANDED, linewidth=1.0, label="fitted model - measured")  # fmt: skip
    bottom.annotate(
        f"shaded: noise floor of the readings, +/- {floor:.3f} deg",
        xy=(0.0, 1.0), xycoords="axes fraction", xytext=(4, -4), textcoords="offset points",
        ha="left", va="top", fontsize=8, color=SECONDARY_INK,
    )  # fmt: skip
    bottom.set_ylabel("error (deg)", color=SECONDARY_INK)
    bottom.set_xlabel("time since the first command (s)", color=SECONDARY_INK)
    legend = bottom.legend(loc="lower right", frameon=False, fontsize=8)
    for text in legend.get_texts():
        text.set_color(SECONDARY_INK)

    verdict = (
        f"fitted model {deg(fitted_error_rad):.3f} deg RMS, default {deg(default_error_rad):.3f}"
        f" deg, noise floor {floor:.3f} deg: fit {fit_quality}"
    )
    figure.suptitle(f"{title}\n{verdict}", color=INK if fit_quality == "good" else CRITICAL,
                    fontsize=11, x=0.01, ha="left")  # fmt: skip
    figure.savefig(path, format="png", facecolor=SURFACE)
    return path


def _style(axes) -> None:
    axes.set_facecolor(SURFACE)
    axes.grid(True, color=GRID, linewidth=0.6)
    axes.set_axisbelow(True)
    for name, spine in axes.spines.items():
        spine.set_visible(name in ("left", "bottom"))
        spine.set_color(MUTED)
    axes.tick_params(colors=MUTED, labelcolor=SECONDARY_INK, labelsize=9)


def _limit_lines(axes, values, lines) -> None:
    """Fix the vertical range to the data, then draw the limits that fall inside it."""
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return
    low, high = min(finite), max(finite)
    pad = max((high - low) * 0.08, 0.5)
    low, high = low - pad, high + pad
    axes.set_ylim(low, high)
    for value, name in lines:
        if low <= value <= high:
            axes.axhline(value, color=MUTED, linewidth=0.9, linestyle=(0, (4, 3)))
            axes.annotate(
                name,
                xy=(1.0, value),
                xycoords=("axes fraction", "data"),
                xytext=(-2, 2),
                textcoords="offset points",
                ha="right",
                va="bottom",
                fontsize=8,
                color=MUTED,
            )
