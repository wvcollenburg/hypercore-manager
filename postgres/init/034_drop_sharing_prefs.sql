-- 034: the Foresight-Central integration (data sharing) is removed in 1.0.0; its per-cluster
-- preferences table goes with it. Idempotent.
DROP TABLE IF EXISTS config.sharing_prefs CASCADE;
