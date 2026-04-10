"""
test_02_db_schema.py
Covers: G3 (PostgreSQL schema tables), §1.5 DB migration verification.
"""
from __future__ import annotations

import pytest
from helpers import db_all, db_scalar


pytestmark = pytest.mark.usefixtures("sentinel_up")


class TestDbSchemas:
    """§1.5 / G3: all expected schemas exist."""

    def test_s1_5_schemas_present(self, db_conn):
        """§1.5: schemas config, recommendations, audit are present."""
        rows = db_all(db_conn, "SELECT schema_name FROM information_schema.schemata")
        names = {r[0] for r in rows}
        for schema in ("config", "recommendations", "audit"):
            assert schema in names, f"Schema '{schema}' missing"


class TestRecommendationsSchema:
    """G3-1: recommendations.* tables."""

    def test_g3_1_recommendations_tables(self, db_conn):
        """G3-1: forecast_snapshots, right_sizing, resource_exhaustion exist."""
        rows = db_all(
            db_conn,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'recommendations'",
        )
        names = {r[0] for r in rows}
        for tbl in ("forecast_snapshots", "right_sizing", "resource_exhaustion"):
            assert tbl in names, f"Table recommendations.{tbl} missing"


class TestConfigSchema:
    """G3-2: config.* tables."""

    def test_g3_2_config_tables(self, db_conn):
        """G3-2: clusters, threshold_overrides, module_toggles, tunables, branding, sharing_prefs."""
        rows = db_all(
            db_conn,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'config'",
        )
        names = {r[0] for r in rows}
        for tbl in (
            "clusters", "threshold_overrides", "module_toggles",
            "tunables", "branding", "sharing_prefs",
        ):
            assert tbl in names, f"Table config.{tbl} missing"


class TestAuditSchema:
    """G3-3: audit.* tables."""

    def test_g3_3_audit_tables(self, db_conn):
        """G3-3: recommendation_events, action_log, webhook_deliveries."""
        rows = db_all(
            db_conn,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'audit'",
        )
        names = {r[0] for r in rows}
        for tbl in ("recommendation_events", "action_log", "webhook_deliveries"):
            assert tbl in names, f"Table audit.{tbl} missing"


class TestPhase2Schema:
    """Phase 2 extra tables."""

    def test_cluster_health_scores_table(self, db_conn):
        """cluster_health_scores table present (Phase 2 migration 007)."""
        count = db_scalar(
            db_conn,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='recommendations' AND table_name='cluster_health_scores'",
        )
        assert count == 1

    def test_resize_recommendations_table(self, db_conn):
        """resize_recommendations table present (Phase 2 migration 007)."""
        count = db_scalar(
            db_conn,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='recommendations' AND table_name='resize_recommendations'",
        )
        assert count == 1


class TestSeverityEnum:
    """ATT-2: severity_level enum includes ATTENTION."""

    def test_att_2_severity_enum_has_attention(self, db_conn):
        """ATT-2: enum_range(severity_level) includes ATTENTION."""
        row = db_all(
            db_conn,
            "SELECT unnest(enum_range(NULL::public.severity_level))::text",
        )
        values = {r[0] for r in row}
        assert "ATTENTION" in values, f"ATTENTION missing from severity enum; got {values}"
        # Verify full set
        for sev in ("INFO", "ATTENTION", "WARNING", "CRITICAL", "EMERGENCY"):
            assert sev in values, f"Severity {sev} missing from enum"


class TestOutcomeTrackingColumns:
    """C7: recommendation_events has Phase 3 outcome columns."""

    def test_c7_outcome_columns(self, db_conn):
        """C7: acted_on_at, action_outcome, post_action_snapshot columns exist."""
        rows = db_all(
            db_conn,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='audit' AND table_name='recommendation_events'",
        )
        cols = {r[0] for r in rows}
        for col in ("acted_on_at", "action_outcome", "post_action_snapshot"):
            assert col in cols, f"Column audit.recommendation_events.{col} missing"


class TestNullableNodeUuid:
    """Migration 009: node_uuid nullable in right_sizing and resize_recommendations."""

    def test_node_uuid_nullable_right_sizing(self, db_conn):
        """node_uuid column in recommendations.right_sizing allows NULL."""
        row = db_all(
            db_conn,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema='recommendations' AND table_name='right_sizing' "
            "AND column_name='node_uuid'",
        )
        assert row, "node_uuid column not found in right_sizing"
        assert row[0][0] == "YES", "node_uuid in right_sizing must be nullable"

    def test_node_uuid_nullable_resize_recommendations(self, db_conn):
        """node_uuid column in recommendations.resize_recommendations allows NULL."""
        row = db_all(
            db_conn,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema='recommendations' AND table_name='resize_recommendations' "
            "AND column_name='node_uuid'",
        )
        assert row, "node_uuid column not found in resize_recommendations"
        assert row[0][0] == "YES", "node_uuid in resize_recommendations must be nullable"
