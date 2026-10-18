"""
Generate a synthetic but realistic dataset for a fleet of grid-scale BESS sites.

Outputs (CSV, in data/raw/):
  sites.csv            - site master data (rated power/energy, LTSA guarantee)
  scada_site.csv       - 15-min site telemetry: price, setpoint, actual power, SoC, comms flag
  tickets.csv          - O&M ticketing export (faults, planned maintenance, grid curtailment)

All data is simulated. Behaviour is modelled on typical European front-of-meter BESS:
price-driven arbitrage dispatch, block-level PCS/BMS/HVAC faults, planned maintenance,
TSO curtailment and SCADA communication gaps.
"""
import numpy as np
import pandas as pd
from pathlib import Path

RNG = np.random.default_rng(42)
OUT = Path(__file__).resolve().parents[1] / "data" / "raw"
OUT.mkdir(parents=True, exist_ok=True)

START, END = "2025-01-01", "2026-01-01"
IDX = pd.date_range(START, END, freq="15min", inclusive="left", tz="Europe/Berlin")
N = len(IDX)
DT_H = 0.25

SITES = pd.DataFrame([
    # id,        name,               country, blocks, MW/block, MWh/block, guarantee %
    ("BAV-01", "Bavaria Storage 1", "DE", 8, 2.5, 5.0, 97.0),
    ("NRW-02", "Rhineland Storage 2", "DE", 10, 2.5, 5.0, 97.0),
    ("NL-03", "Groningen Storage 3", "NL", 12, 2.5, 5.0, 97.5),
], columns=["site_id", "site_name", "country", "n_blocks", "block_mw", "block_mwh", "guaranteed_availability_pct"])
SITES["rated_mw"] = SITES.n_blocks * SITES.block_mw
SITES["rated_mwh"] = SITES.n_blocks * SITES.block_mwh
SITES["ld_rate_eur_per_mw_per_pct"] = 450.0  # LD per MW per %-point shortfall per month
SITES["planned_maint_allowance_h"] = 72.0     # excused planned maintenance per block per year

FAULTS = [
    # category, component, rate per block-year, mean duration h, sd(log)
    ("PCS trip", "PCS", 6.0, 3.0, 0.8),
    ("BMS cell imbalance", "BMS", 3.0, 10.0, 0.9),
    ("HVAC failure", "HVAC", 2.0, 20.0, 0.7),
    ("Fire detection alarm", "FSS", 0.5, 30.0, 0.6),
    ("EMS communication loss", "EMS", 2.5, 2.0, 0.9),
]


def day_ahead_price():
    """Synthetic day-ahead price (EUR/MWh): duck curve + seasonality + noise + spikes."""
    h = IDX.hour + IDX.minute / 60
    doy = IDX.dayofyear.values
    season = 15 * np.cos(2 * np.pi * (doy - 15) / 365)  # winter higher
    solar_dip = -40 * np.exp(-((h - 13) ** 2) / 6) * (0.6 + 0.4 * np.sin(2 * np.pi * (doy - 80) / 365))
    peaks = 35 * np.exp(-((h - 8) ** 2) / 2) + 55 * np.exp(-((h - 19) ** 2) / 3)
    base = 85 + season + solar_dip + peaks
    noise = RNG.normal(0, 8, N)
    spikes = (RNG.random(N) < 0.002) * RNG.uniform(80, 300, N)
    return np.round(base + noise + spikes, 2)


def make_events(site):
    rows = []
    t0, t1 = IDX[0], IDX[-1]
    span_h = (t1 - t0).total_seconds() / 3600
    for b in range(1, site.n_blocks + 1):
        block_id = f"{site.site_id}-B{b:02d}"
        # Unplanned faults
        for cat, comp, rate, mean_h, sdl in FAULTS:
            n = RNG.poisson(rate)
            for _ in range(n):
                start = t0 + pd.Timedelta(hours=RNG.uniform(0, span_h))
                dur = RNG.lognormal(np.log(mean_h), sdl)
                rows.append((block_id, cat, comp, "unplanned", start, start + pd.Timedelta(hours=dur)))
        # Planned maintenance: two windows per year, ~30-50 h each (some blocks overrun allowance)
        for m in (4, 10):
            start = pd.Timestamp(f"2025-{m:02d}-{RNG.integers(3, 25):02d} 08:00", tz="Europe/Berlin")
            dur = RNG.uniform(28, 52)
            rows.append((block_id, "Planned maintenance", "ALL", "planned", start, start + pd.Timedelta(hours=dur)))
    # Site-level TSO curtailment (excused): affects all blocks
    for _ in range(RNG.poisson(4)):
        start = t0 + pd.Timedelta(hours=RNG.uniform(0, span_h))
        dur = RNG.uniform(2, 10)
        for b in range(1, site.n_blocks + 1):
            rows.append((f"{site.site_id}-B{b:02d}", "TSO curtailment", "GRID", "grid", start, start + pd.Timedelta(hours=dur)))
    # One bad month for NRW-02: recurring PCS firmware issue after an update (realistic story)
    if site.site_id == "NRW-02":
        for b in RNG.choice(range(1, site.n_blocks + 1), 6, replace=False):
            for _ in range(4):
                start = pd.Timestamp("2025-07-01", tz="Europe/Berlin") + pd.Timedelta(hours=RNG.uniform(0, 29 * 24))
                dur = RNG.uniform(10, 30)
                rows.append((f"{site.site_id}-B{b:02d}", "PCS trip (firmware 3.2.1)", "PCS", "unplanned", start, start + pd.Timedelta(hours=dur)))
    ev = pd.DataFrame(rows, columns=["block_id", "category", "component", "event_class", "start_ts", "end_ts"])
    ev["site_id"] = site.site_id
    ev["start_ts"] = ev.start_ts.dt.floor("15min")
    ev["end_ts"] = ev.end_ts.clip(upper=pd.Timestamp(END, tz="Europe/Berlin")).dt.ceil("15min")
    return ev


def block_availability_fraction(site, ev):
    """Fraction of site blocks available in each interval (any event -> block down)."""
    down = np.zeros((site.n_blocks, N), dtype=bool)
    pos = {f"{site.site_id}-B{b:02d}": b - 1 for b in range(1, site.n_blocks + 1)}
    s = IDX.searchsorted(ev.start_ts)
    e = IDX.searchsorted(ev.end_ts)
    phys = (ev.component != "EMS").values  # comms loss is a data issue, not physical downtime
    for bid, i, j, ph in zip(ev.block_id, s, e, phys):
        if not ph:
            continue
        down[pos[bid], i:j] = True
    return 1 - down.mean(axis=0)


def dispatch(site, price, avail_frac):
    """Simple price-threshold arbitrage: charge in cheapest hours, discharge in priciest, per day."""
    p_max = site.rated_mw * avail_frac
    e_max = site.rated_mwh
    soc = 0.5 * e_max
    eta = 0.93  # one-way efficiency
    setpoint = np.zeros(N)
    actual = np.zeros(N)
    soc_arr = np.zeros(N)
    days = IDX.normalize()
    day_codes, day_starts = np.unique(days.values, return_index=True)
    bounds = list(day_starts) + [N]
    for d in range(len(day_starts)):
        a, b = bounds[d], bounds[d + 1]
        pr = price[a:b]
        lo, hi = np.quantile(pr, 0.22), np.quantile(pr, 0.80)
        for k in range(a, b):
            if price[k] <= lo:
                sp = -p_max[k]
            elif price[k] >= hi:
                sp = p_max[k]
            else:
                sp = 0.0
            # SoC limits 5-95 %
            if sp < 0:
                sp = max(sp, -(0.95 * e_max - soc) / (DT_H * eta))
            elif sp > 0:
                sp = min(sp, (soc - 0.05 * e_max) * eta / DT_H)
            act = sp * (1 + RNG.normal(0, 0.01)) if sp != 0 else RNG.normal(0, 0.02)
            # occasional setpoint tracking deviation (EMS/PCS ramp issue)
            if sp != 0 and RNG.random() < 0.01:
                act = sp * RNG.uniform(0.6, 0.9)
            soc -= (act / eta if act > 0 else act * eta) * DT_H
            soc = float(np.clip(soc, 0.0, e_max))
            setpoint[k], actual[k], soc_arr[k] = sp, act, soc
    return np.round(setpoint, 3), np.round(actual, 3), np.round(100 * soc_arr / e_max, 2)


def comms_gaps(n_gaps):
    """Return boolean mask of intervals with missing SCADA data."""
    mask = np.zeros(N, dtype=bool)
    for _ in range(n_gaps):
        i = RNG.integers(0, N - 200)
        length = int(RNG.choice([1, 2, 3, 4, 8, 16, 40], p=[.3, .2, .15, .12, .1, .08, .05]))
        mask[i:i + length] = True
    return mask


def main():
    price = day_ahead_price()
    scada, tickets = [], []
    for site in SITES.itertuples():
        ev = make_events(site)
        avail = block_availability_fraction(site, ev)
        sp, act, soc = dispatch(site, price, avail)
        gap = comms_gaps(60)
        # Most EMS communication-loss tickets also leave a hole in the SCADA feed
        ems = ev[ev.component == "EMS"].drop_duplicates("start_ts")
        for st, en in zip(IDX.searchsorted(ems.start_ts), IDX.searchsorted(ems.end_ts)):
            if RNG.random() < 0.8:
                gap[st:max(en, st + 5)] = True
        df = pd.DataFrame({
            "ts": IDX, "site_id": site.site_id, "da_price_eur_mwh": price,
            "p_setpoint_mw": sp, "p_actual_mw": act, "soc_pct": soc,
            "available_mw_reported": np.round(site.rated_mw * avail, 3),
        })
        df.loc[gap, ["p_actual_mw", "soc_pct", "available_mw_reported"]] = np.nan
        df["comms_ok"] = (~gap).astype(int)
        scada.append(df)
        # Tickets: EMS comms losses are logged but don't take the block down physically in reality;
        # keep them as tickets (they matter for data-gap reconciliation).
        tickets.append(ev)
    scada = pd.concat(scada)
    tickets = pd.concat(tickets).sort_values("start_ts").reset_index(drop=True)
    tickets.insert(0, "ticket_id", [f"TCK-{i:05d}" for i in range(1, len(tickets) + 1)])
    tickets["duration_h"] = ((tickets.end_ts - tickets.start_ts).dt.total_seconds() / 3600).round(2)

    SITES.to_csv(OUT / "sites.csv", index=False)
    scada.to_csv(OUT / "scada_site.csv", index=False)
    tickets.to_csv(OUT / "tickets.csv", index=False)
    print(f"sites: {len(SITES)}  scada rows: {len(scada):,}  tickets: {len(tickets):,}")


if __name__ == "__main__":
    main()
