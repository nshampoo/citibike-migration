"""The daily tide: net bikes riders move into each zone, cumulative from midnight.

Reads data/trips.duckdb (built by analysis/prepare.sql) and writes a 16:9 PNG for slides.
Usage: .venv/bin/python analysis/tide_chart.py
"""
import pathlib

import duckdb
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

# Reference palette (dataviz skill), categorical slots 1-3, light mode.
SURFACE, INK, INK_2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
ZONES = {  # zone -> (label, color); order fixes the color slot
    "fills": ("Manhattan areas that fill on weekday mornings", "#2a78d6"),
    "empties": ("Manhattan areas that empty on weekday mornings", "#eb6834"),
    "outer": ("Brooklyn, Queens & Bronx", "#1baf7a"),
}

QUERY = """
WITH role AS (
  SELECT area, borough,
    CASE WHEN borough <> 'Manhattan' THEN 'outer'
         WHEN sum(trip_net) FILTER (WHERE hour(hour) BETWEEN 6 AND 10 AND dayofweek(hour) BETWEEN 1 AND 5) > 0 THEN 'fills'
         ELSE 'empties' END AS zone
  FROM hourly_net_flow
  WHERE borough IN ('Manhattan', 'Brooklyn', 'Queens', 'Bronx')  -- NYC's tide; New Jersey is its own story
  GROUP BY area, borough
),
q AS (  -- net bikes per zone per 15 minutes per day
  SELECT r.zone, e.ts::date AS dt, dayofweek(e.ts) IN (0, 6) AS weekend,
         (hour(e.ts) * 60 + minute(e.ts)) // 15 AS slot, sum(e.d) AS n
  FROM events e JOIN station_area a USING (sid) JOIN role r USING (area, borough)
  WHERE e.ts >= '2026-08-01' AND e.ts < '2026-09-01'
  GROUP BY ALL
),
grid AS (  -- fill empty slots with 0 so the cumulative sum is right
  SELECT z.zone, d.dt, dayofweek(d.dt) IN (0, 6) AS weekend, s.slot
  FROM (SELECT DISTINCT zone FROM role) z,
       (SELECT DISTINCT dt FROM q) d,
       range(96) s(slot)
),
cum AS (
  SELECT g.zone, g.weekend, g.slot,
         sum(coalesce(q.n, 0)) OVER (PARTITION BY g.zone, g.dt ORDER BY g.slot) AS c
  FROM grid g LEFT JOIN q USING (zone, dt, slot)
)
SELECT zone, weekend, slot, avg(c) AS bikes FROM cum GROUP BY ALL ORDER BY zone, weekend, slot
"""


def main() -> None:
    df = duckdb.connect(str(ROOT / "data" / "trips.duckdb"), read_only=True).sql(QUERY).df()
    df["hour"] = (df["slot"] + 1) / 4  # value at the end of each 15-min slot
    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / "tide_2026-08.csv", index=False)

    plt.rcParams.update({"font.family": "Helvetica Neue", "font.size": 13, "text.color": INK})
    fig, axes = plt.subplots(1, 2, figsize=(16, 9), sharey=True, facecolor=SURFACE,
                             gridspec_kw={"width_ratios": [1, 1], "wspace": 0.08})
    for ax, weekend, title in [(axes[0], False, "Weekdays"), (axes[1], True, "Weekends")]:
        ax.set_facecolor(SURFACE)
        ax.axhline(0, color=MUTED, lw=1)
        for zone, (label, color) in ZONES.items():
            s = df[(df.zone == zone) & (df.weekend == weekend)]
            ax.plot(s.hour, s.bikes, color=color, lw=2.5, solid_capstyle="round")
            if not weekend:  # direct labels on the weekday panel, where lines separate
                peak = s.loc[s.bikes.abs().idxmax()]
                ax.annotate(f"{peak.bikes:+,.0f}", (peak.hour, peak.bikes), xytext=(0, 10) if peak.bikes > 0 else (-34, -20),
                            textcoords="offset points", ha="center", color=INK_2, fontsize=12)
        ax.set_title(title, loc="left", fontsize=15, color=INK, fontweight="bold")
        ax.set_xlim(0, 24)
        ticks = [0, 6, 12, 18] + ([24] if weekend else [])  # don't double the shared midnight label
        ax.set_xticks(ticks, ["12am", "6am", "12pm", "6pm", "12am"][: len(ticks)])
        ax.grid(axis="y", color=GRID, lw=1)
        ax.tick_params(colors=INK_2, length=0)
        for side in ax.spines.values():
            side.set_visible(False)
    axes[0].yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+,.0f}" if v else "0"))
    axes[0].set_ylabel("Net bikes ridden in since midnight (avg day)", color=INK_2)

    fig.suptitle("Every weekday, Citi Bike riders move ~5,000 bikes into Manhattan's work areas, and back by night",
                 x=0.07, ha="left", fontsize=19, fontweight="bold", y=0.97)
    fig.text(0.07, 0.905, "Net arrivals minus departures, per zone, averaged over August 2026. Truck rebalancing not included. Source: Citi Bike System Data.",
             color=INK_2, fontsize=13)
    handles = [plt.Line2D([], [], color=c, lw=3) for _, c in ZONES.values()]
    fig.legend(handles, [l for l, _ in ZONES.values()], loc="lower left", bbox_to_anchor=(0.065, 0.01),
               ncol=3, frameon=False, fontsize=12.5, labelcolor=INK_2)
    fig.subplots_adjust(left=0.07, right=0.93, top=0.84, bottom=0.14)
    fig.savefig(OUT / "tide_2026-08.png", dpi=150, facecolor=SURFACE)
    print(df[df.weekend == False].groupby("zone").bikes.agg(["min", "max"]))


if __name__ == "__main__":
    main()
