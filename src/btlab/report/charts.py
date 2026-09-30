"""Plotly figures shared by the HTML reports and the Streamlit pages."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from btlab.data.quality import STATUS_LABELS, WEEKDAY_LABELS

# Reference palette (dataviz skill): sequential blue ramp, status colours, chrome.
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
STATUS_COLORS = {
    "valid": "#0ca30c",
    "invalid": "#d03b3b",
    "excluded_low_liquidity": "#fab219",
    "holiday": "#898781",
    "off_session": "#c3c2b7",
    "partial": "#e1e0d9",
}
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e1e0d9", "#fcfcfb"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def _layout(fig: go.Figure, title: str, height: int) -> go.Figure:
    fig.update_layout(
        title={"text": title, "font": {"size": 15, "color": INK}},
        font={"family": FONT, "color": INK_2, "size": 12},
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        height=height,
        margin={"l": 50, "r": 20, "t": 50, "b": 40},
        hoverlabel={"font": {"family": FONT}},
    )
    fig.update_xaxes(gridcolor=GRID, linecolor="#c3c2b7", zeroline=False)
    fig.update_yaxes(gridcolor=GRID, linecolor="#c3c2b7", zeroline=False)
    return fig


def coverage_heatmap(heat: pd.DataFrame, column: str, title: str, tz: str) -> go.Figure:
    """Share of weeks with a quote at each (weekday, hour), in the session timezone."""
    grid = heat.pivot(index="weekday", columns="hour", values=column).reindex(range(7))
    fig = go.Figure(
        go.Heatmap(
            z=grid.to_numpy(),
            x=[f"{h:02d}h" for h in grid.columns],
            y=WEEKDAY_LABELS,
            zmin=0,
            zmax=1,
            colorscale=[[i / (len(SEQ_BLUE) - 1), c] for i, c in enumerate(SEQ_BLUE)],
            xgap=2,
            ygap=2,
            colorbar={"title": "part", "tickformat": ".0%"},
            hovertemplate="%{y} %{x} : %{z:.0%}<extra></extra>",
        )
    )
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(showgrid=False, title=f"heure locale ({tz})")
    return _layout(fig, title, 300)


def status_by_year(per_year: list[dict]) -> go.Figure:
    df = pd.DataFrame(per_year)
    fig = go.Figure()
    if df.empty:
        return _layout(fig, "Statut des jours par année", 320)
    for status, label in STATUS_LABELS.items():
        if status not in df or not df[status].any():
            continue
        fig.add_bar(
            x=df["year"].astype(str),
            y=df[status],
            name=label,
            marker={"color": STATUS_COLORS[status], "line": {"color": SURFACE, "width": 1}},
            hovertemplate=f"%{{x}} · {label} : %{{y}} jours<extra></extra>",
        )
    fig.update_layout(barmode="stack", bargap=0.25, legend={"orientation": "h", "y": -0.2})
    fig.update_yaxes(title="jours")
    return _layout(fig, "Statut des jours par année", 360)
