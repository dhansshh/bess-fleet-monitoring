-- Monthly operational performance KPIs per site, from cleaned 15-min SCADA telemetry.
-- Energy in MWh (15-min interval = 0.25 h). Positive power = discharge, negative = charge.
DROP VIEW IF EXISTS v_performance_monthly;
CREATE VIEW v_performance_monthly AS
SELECT
    s.site_id,
    substr(s.ts, 1, 7)                                                     AS month,
    ROUND(SUM(CASE WHEN s.p_actual_mw > 0 THEN  s.p_actual_mw END) * 0.25, 1) AS discharged_mwh,
    ROUND(SUM(CASE WHEN s.p_actual_mw < 0 THEN -s.p_actual_mw END) * 0.25, 1) AS charged_mwh,
    ROUND(100.0 * SUM(CASE WHEN s.p_actual_mw > 0 THEN s.p_actual_mw END)
               / NULLIF(SUM(CASE WHEN s.p_actual_mw < 0 THEN -s.p_actual_mw END), 0), 1) AS round_trip_eff_pct,
    ROUND(SUM(CASE WHEN s.p_actual_mw > 0 THEN s.p_actual_mw END) * 0.25 / m.rated_mwh, 1) AS equiv_full_cycles,
    ROUND(SUM(s.da_price_eur_mwh * s.p_actual_mw * 0.25) / 1000.0, 1)       AS arbitrage_revenue_keur,
    -- Setpoint tracking: share of active intervals where actual deviates > 5 % of rated power
    ROUND(100.0 * SUM(CASE WHEN s.p_setpoint_mw <> 0
                           AND ABS(s.p_actual_mw - s.p_setpoint_mw) > 0.05 * m.rated_mw THEN 1 ELSE 0 END)
               / NULLIF(SUM(CASE WHEN s.p_setpoint_mw <> 0 THEN 1 ELSE 0 END), 0), 2) AS tracking_violation_pct,
    ROUND(100.0 * AVG(s.comms_ok), 2)                                         AS data_completeness_pct
FROM scada_clean s
JOIN sites m USING (site_id)
GROUP BY s.site_id, month;

-- Fault Pareto: downtime block-hours by category (physical events only)
DROP VIEW IF EXISTS v_fault_pareto;
CREATE VIEW v_fault_pareto AS
SELECT site_id, category, event_class,
       COUNT(*)                 AS tickets,
       ROUND(SUM(duration_h),1) AS block_hours,
       ROUND(AVG(duration_h),1) AS mttr_h
FROM tickets
WHERE component <> 'EMS'
GROUP BY site_id, category, event_class;
