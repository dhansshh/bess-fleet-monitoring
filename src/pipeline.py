"""
BESS fleet data pipeline.

1. Ingest raw SCADA telemetry, tickets and site master data into SQLite (data/bess_fleet.db).
2. Validate and clean SCADA: detect communication gaps, fill short gaps, flag long gaps and
   reconcile them against the ticketing system.
3. Calculate contractual (LTSA) availability per site and month, applying excusable events:
     - TSO / grid curtailment          -> excused
     - planned maintenance              -> excused up to the annual allowance per block
     - everything else (unplanned)      -> counts against availability
4. Calculate liquidated damages (LDs) for months below the guaranteed availability.
5. Build performance KPIs (SQL views) and export everything for the dashboard (output/).
"""
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "bess_fleet.db"
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
TZ = "Europe/Berlin"
SHORT_GAP_MAX = 4  # intervals (= 1 h): forward-fill; longer gaps are reconciled with tickets


# ---------------------------------------------------------------- 1. ingest
def load_raw():
    sites = pd.read_csv(RAW / "sites.csv")
    scada = pd.read_csv(RAW / "scada_site.csv")
    scada["ts"] = pd.to_datetime(scada.ts, utc=True).dt.tz_convert(TZ)
    tickets = pd.read_csv(RAW / "tickets.csv")
    for c in ("start_ts", "end_ts"):
        tickets[c] = pd.to_datetime(tickets[c], utc=True).dt.tz_convert(TZ)
    return sites, scada, tickets


# ---------------------------------------------------------------- 2. validate & clean
def validate(sites, scada):
    """Basic data-quality checks; raises on hard failures, returns a report dict."""
    rep = {}
    dup = scada.duplicated(["site_id", "ts"]).sum()
    assert dup == 0, f"{dup} duplicate timestamps"
    for sid, g in scada.groupby("site_id"):
        diffs = g.ts.diff().dropna().unique()
        assert len(diffs) == 1, f"{sid}: irregular sampling {diffs}"
    rated = scada.site_id.map(sites.set_index("site_id").rated_mw)
    over = (scada.p_actual_mw.abs() > rated * 1.05).sum()
    soc_bad = ((scada.soc_pct < 0) | (scada.soc_pct > 100)).sum()
    rep["power_above_rating"] = int(over)
    rep["soc_out_of_range"] = int(soc_bad)
    return rep


def run_lengths(mask):
    """Return (start_idx, length) of True runs in a boolean array."""
    m = np.concatenate([[False], mask, [False]]).astype(int)
    d = np.diff(m)
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    return starts, ends - starts


def clean_scada(scada, tickets):
    cols = ["p_actual_mw", "soc_pct", "available_mw_reported"]
    out, gap_log = [], []
    for sid, g in scada.groupby("site_id", sort=False):
        g = g.sort_values("ts").reset_index(drop=True)
        g["gap_fill"] = "none"
        starts, lens = run_lengths(g.p_actual_mw.isna().values)
        site_tk = tickets[(tickets.site_id == sid) & (tickets.component == "EMS")]
        for s, n in zip(starts, lens):
            t0, t1 = g.ts[s], g.ts[s + n - 1] + pd.Timedelta("15min")
            if n <= SHORT_GAP_MAX:
                method = "forward_fill"
            else:
                hit = site_tk[(site_tk.start_ts < t1) & (site_tk.end_ts > t0)]
                method = "reconciled_ticket" if len(hit) else "interpolated_unexplained"
            g.loc[s:s + n - 1, "gap_fill"] = method
            gap_log.append({"site_id": sid, "gap_start": t0, "gap_end": t1,
                            "intervals": int(n), "hours": n / 4, "method": method})
        # Fill: short -> ffill; long -> linear interpolation for SoC, setpoint for power
        g[cols] = g[cols].ffill(limit=SHORT_GAP_MAX)
        g["soc_pct"] = g.soc_pct.interpolate(limit_direction="both")
        g["p_actual_mw"] = g.p_actual_mw.fillna(g.p_setpoint_mw)
        g["available_mw_reported"] = g.available_mw_reported.ffill()
        out.append(g)
    return pd.concat(out, ignore_index=True), pd.DataFrame(gap_log)


# ---------------------------------------------------------------- 3. availability
def availability(sites, tickets, idx):
    """
    Interval-based contractual availability.
    For every block and 15-min interval assign a single state with priority
    grid (excused) > unplanned (counted) > planned (excused within allowance).
    Overlapping tickets are therefore never double-counted.
    """
    phys = tickets[tickets.component != "EMS"]
    pri = {"grid": 3, "unplanned": 2, "planned": 1}
    rows = []
    for site in sites.itertuples():
        nb = site.n_blocks
        state = np.zeros((nb, len(idx)), dtype=np.int8)
        tk = phys[phys.site_id == site.site_id]
        bpos = tk.block_id.str[-2:].astype(int).values - 1
        s = idx.searchsorted(tk.start_ts)
        e = idx.searchsorted(tk.end_ts)
        for b, i, j, cls in zip(bpos, s, e, tk.event_class.map(pri)):
            state[b, i:j] = np.maximum(state[b, i:j], cls)
        # planned maintenance above annual allowance becomes counted downtime
        allowance_int = int(site.planned_maint_allowance_h * 4)
        planned = state == 1
        cum = np.cumsum(planned, axis=1)
        excess_planned = planned & (cum > allowance_int)

        counted = (state == 2) | excess_planned
        excused = (state == 3) | (planned & ~excess_planned)
        month = idx.strftime("%Y-%m")
        df = pd.DataFrame({
            "month": month,
            "counted_bh": counted.sum(0) / 4,
            "excused_bh": excused.sum(0) / 4,
            "unplanned_bh": (state == 2).sum(0) / 4,
            "planned_bh": planned.sum(0) / 4,
            "planned_excess_bh": excess_planned.sum(0) / 4,
            "grid_bh": (state == 3).sum(0) / 4,
        })
        m = df.groupby("month").sum()
        m["period_bh"] = df.groupby("month").size() * nb / 4
        # Contract formula: A = (Period - Counted - Excused) / (Period - Excused)
        m["availability_pct"] = 100 * (m.period_bh - m.counted_bh - m.excused_bh) / (m.period_bh - m.excused_bh)
        m["technical_availability_pct"] = 100 * (m.period_bh - m.counted_bh - m.excused_bh) / m.period_bh
        m["guaranteed_pct"] = site.guaranteed_availability_pct
        m["shortfall_pct"] = (m.guaranteed_pct - m.availability_pct).clip(lower=0)
        m["ld_eur"] = (m.shortfall_pct * site.rated_mw * site.ld_rate_eur_per_mw_per_pct).round(0)
        m["site_id"] = site.site_id
        rows.append(m.reset_index())
    out = pd.concat(rows, ignore_index=True)
    return out.round(3)


# ---------------------------------------------------------------- main
def main():
    sites, scada, tickets = load_raw()
    dq = validate(sites, scada)
    clean, gaps = clean_scada(scada, tickets)

    con = sqlite3.connect(DB)
    sites.to_sql("sites", con, if_exists="replace", index=False)
    tk = tickets.copy()
    for c in ("start_ts", "end_ts"):
        tk[c] = tk[c].dt.strftime("%Y-%m-%d %H:%M:%S%z")
    tk.to_sql("tickets", con, if_exists="replace", index=False)
    cs = clean.copy()
    cs["ts"] = cs.ts.dt.strftime("%Y-%m-%d %H:%M:%S%z")
    cs.to_sql("scada_clean", con, if_exists="replace", index=False)
    con.executescript((ROOT / "sql" / "performance_kpis.sql").read_text())

    idx = pd.date_range("2025-01-01", "2026-01-01", freq="15min", inclusive="left", tz=TZ)
    av = availability(sites, tickets, idx)
    av.to_sql("availability_monthly", con, if_exists="replace", index=False)

    perf = pd.read_sql("SELECT * FROM v_performance_monthly", con)
    pareto = pd.read_sql("SELECT * FROM v_fault_pareto ORDER BY block_hours DESC", con)
    con.close()

    # Daily site profile for dashboard drill-down (hourly means keep the file small)
    hourly = (clean.set_index("ts").groupby("site_id")
              [["da_price_eur_mwh", "p_setpoint_mw", "p_actual_mw", "soc_pct", "available_mw_reported"]]
              .resample("1h").mean().round(2).reset_index())

    gap_summary = (gaps.groupby(["site_id", "method"])
                   .agg(gaps=("hours", "size"), hours=("hours", "sum")).reset_index())
    dq_summary = {
        **dq,
        "scada_rows": int(len(scada)),
        "missing_intervals": int(scada.p_actual_mw.isna().sum()),
        "completeness_pct": round(100 * scada.p_actual_mw.notna().mean(), 2),
        "gaps_total": int(len(gaps)),
        "gaps_by_method": gaps.method.value_counts().to_dict(),
    }

    bundle = {
        "generated": pd.Timestamp.now(tz=TZ).isoformat(timespec="minutes"),
        "sites": sites.to_dict("records"),
        "availability": av.to_dict("records"),
        "performance": perf.to_dict("records"),
        "pareto": pareto.to_dict("records"),
        "gaps": gap_summary.to_dict("records"),
        "data_quality": dq_summary,
        "hourly": {
            sid: {
                "ts": g.ts.dt.strftime("%Y-%m-%dT%H").tolist(),
                "price": g.da_price_eur_mwh.tolist(),
                "sp": g.p_setpoint_mw.tolist(),
                "act": g.p_actual_mw.tolist(),
                "soc": g.soc_pct.tolist(),
                "avail": g.available_mw_reported.tolist(),
            } for sid, g in hourly.groupby("site_id")
        },
    }
    (OUT / "dashboard_data.json").write_text(json.dumps(bundle, default=str))
    av.to_csv(OUT / "availability_monthly.csv", index=False)
    perf.to_csv(OUT / "performance_monthly.csv", index=False)
    gaps.to_csv(OUT / "data_gaps_log.csv", index=False)

    # Console summary
    yr = av.groupby("site_id").agg(avail=("availability_pct", "mean"), ld=("ld_eur", "sum"),
                                   months_below=("shortfall_pct", lambda s: int((s > 0).sum())))
    print("Data quality:", json.dumps(dq_summary, indent=1))
    print(yr.round(2))
    print(perf.groupby("site_id")[["discharged_mwh", "equiv_full_cycles", "arbitrage_revenue_keur"]].sum())


if __name__ == "__main__":
    main()
