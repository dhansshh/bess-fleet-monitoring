# BESS Fleet Monitor

An end-to-end data project for a grid-scale **battery energy storage (BESS) fleet**: from raw SCADA telemetry and O&M tickets to contractual availability, liquidated damages (LDs) and an interactive operations dashboard.

> **Data note:** all telemetry, tickets, prices and contract terms are **simulated** (see `src/generate_data.py`). The behaviour is modelled on typical European front-of-meter BESS: price-driven arbitrage, block-level PCS/BMS/HVAC faults, planned maintenance, TSO curtailment and SCADA communication gaps. No real asset is described.

## The question

An LTSA (long-term service agreement) guarantees a monthly availability per site. If availability falls below the guarantee, the service provider pays liquidated damages. The operations team needs to know:

1. What was each site's **contractual** availability, after excusing events the contract allows?
2. Which months triggered **LDs**, and how much?
3. Which **fault categories** drive the downtime?
4. Is the **SCADA data** complete enough to trust the numbers, and where are the gaps?
5. Is the EMS **dispatching** as intended (setpoint tracking, round-trip efficiency, cycles)?

## Fleet (simulated)

| Site | Country | Blocks | Rated | Guarantee |
|---|---|---|---|---|
| BAV-01 Bavaria Storage 1 | DE | 8 × 2.5 MW / 5 MWh | 20 MW / 40 MWh | 97.0 % |
| NRW-02 Rhineland Storage 2 | DE | 10 × 2.5 MW / 5 MWh | 25 MW / 50 MWh | 97.0 % |
| NL-03 Groningen Storage 3 | NL | 12 × 2.5 MW / 5 MWh | 30 MW / 60 MWh | 97.5 % |

Period: calendar year 2025, 15-minute resolution (105,120 SCADA rows, ~600 tickets).

## Pipeline

```
generate_data.py  ->  data/raw/*.csv
pipeline.py       ->  data/bess_fleet.db (SQLite) + output/*.csv + output/dashboard_data.json
build_dashboard.py->  output/dashboard.html
```

| Step | What happens |
|---|---|
| Ingest | SCADA CSV, ticket export and site master data loaded into SQLite |
| Validate | duplicate timestamps, regular 15-min sampling, power above rating, SoC within 0–100 % |
| Clean | gap detection; gaps ≤ 1 h forward-filled; longer gaps matched to EMS communication-loss tickets, unmatched gaps flagged |
| Availability | interval-level state per block and 15 min (no double counting of overlapping tickets) |
| LDs | shortfall × rated MW × €450 per MW per %-point, per month |
| KPIs | SQL views (`sql/performance_kpis.sql`): energy throughput, round-trip efficiency, equivalent full cycles, arbitrage margin, setpoint-tracking violations, data completeness, fault Pareto with MTTR |

### Availability formula

```
A = (T − D_counted − D_excused) / (T − D_excused)
```

- `T` = period block-hours
- **Excused:** TSO curtailment; planned maintenance up to 72 h per block per year
- **Counted:** unplanned faults; planned maintenance beyond the allowance
- State priority per interval: grid curtailment > unplanned fault > planned maintenance

## Results (2025, simulated)

| Site | Availability (mean of months) | Months below guarantee | LDs |
|---|---|---|---|
| BAV-01 | 98.24 % | 2 | €3,645 |
| NRW-02 | 97.53 % | 1 | €69,866 |
| NL-03 | 98.48 % | 1 | €8,684 |

- **Main finding:** NRW-02 fell to 90.8 % in July 2025 after a PCS firmware update (3.2.1) caused repeated trips on six blocks. That one month accounts for 85 % of the fleet's €82k LD exposure.
- Fleet SCADA completeness: 98.3 %; 225 gaps, of which 47 could not be explained by a ticket and were flagged.
- Round-trip efficiency ≈ 87 %, about 460 equivalent full cycles per site.

## Dashboard

`output/dashboard.html` is a single self-contained file (no server needed). It shows:

- KPI strip: fleet availability, LD exposure, arbitrage margin, SCADA completeness
- Monthly availability vs guarantee per site (small multiples), LD ledger, downtime split
- Fault Pareto, monthly performance table, daily dispatch profile (power / SoC / price) with day picker
- Data-quality and gap-reconciliation table, plus the method

Open it in a browser, or host it with GitHub Pages.

## Run it

```bash
pip install -r requirements.txt
python src/generate_data.py
python src/pipeline.py
python src/build_dashboard.py
```

Query the database directly:

```bash
sqlite3 data/bess_fleet.db "SELECT * FROM v_performance_monthly WHERE site_id='NRW-02';"
```

## Stack

Python (pandas, NumPy) · SQLite · SQL views · HTML/SVG dashboard (no framework)

## Possible extensions

- Replace the simulated price with real day-ahead prices (e.g. ENTSO-E Transparency Platform API)
- Load into Power BI or Grafana from the SQLite database / CSV exports
- Move storage to a cloud database (Azure SQL / AWS RDS) and schedule the pipeline
- Battery state-of-health tracking and degradation forecasting
