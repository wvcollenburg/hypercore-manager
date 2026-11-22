"""dashboard/reports/pdf_report.py — the Foresight PDF report (sentinel/reports keeps an identical copy).

Needs reportlab, which the dashboard image has and the plain test image does not: run with the dashboard
image (skipped elsewhere)."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip('reportlab')

import reportlab.platypus as platypus     # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('pdf_report', ROOT / 'dashboard' / 'reports' / 'pdf_report.py')
pdf_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pdf_report)


def test_both_copies_are_identical():
    assert (ROOT / 'dashboard' / 'reports' / 'pdf_report.py').read_bytes() == \
           (ROOT / 'sentinel' / 'reports' / 'pdf_report.py').read_bytes()


def _data(**kw):
    exhaustion = [
        {'resource_dimension': f'dim{i}', 'days_until_exhaustion': d, 'exhaustion_date': '2027-01-01',
         'severity': 'WARNING', 'node_uuid': None, 'current_usage_pct': 50.0, 'growth_rate_per_day': 0.1}
        for i, d in enumerate([200, 100, 300, 150, 120, 90])
    ] + [{'resource_dimension': 'ram', 'days_until_exhaustion': 0, 'exhaustion_date': '2026-11-22',
          'severity': 'EMERGENCY', 'node_uuid': None, 'current_usage_pct': 104.0, 'growth_rate_per_day': 1.0}]
    forecasts = [{'forecast_horizon_days': 30, 'node_uuid': n, 'resource_dimension': 'cpu', 'current_usage': 30.0,
                  'forecast_value': 32.0, 'severity': 'INFO', 'exhaustion_date': None}
                 for n in ('aaaaaaaa-1', 'bbbbbbbb-2', 'cccccccc-3', None)]
    d = {'cluster_id': 'c1', 'generated_at': '2026-11-22T10:00:00Z', 'health': {'health_score': 80, 'severity': 'INFO'},
         'forecasts': forecasts, 'rightsizing': [], 'resize': [], 'exhaustion': exhaustion,
         'branding': {'company_name': 'R<D Labs & AT&T'}}
    d.update(kw)
    return d


@pytest.fixture
def captured(monkeypatch):
    texts, cards = [], []
    real_p = platypus.Paragraph

    class P(real_p):
        def __init__(self, text, *a, **k):
            texts.append(text)
            super().__init__(text, *a, **k)
    monkeypatch.setattr(platypus, 'Paragraph', P)
    real_card = pdf_report._kpi_card
    monkeypatch.setattr(pdf_report, '_kpi_card', lambda label, value, *a, **k: (cards.append((label, value)), real_card(label, value, *a, **k))[1])
    return texts, cards


def test_a_company_name_with_markup_characters_is_printed_as_written(captured):
    """'R<D Labs' made every report a 500 (reportlab parses Paragraph text as markup); 'AT&T' printed as 'AT'."""
    texts, _ = captured
    assert pdf_report.generate(_data())[:4] == b'%PDF'
    assert any('R&lt;D Labs &amp; AT&amp;T' in t for t in texts)


def test_an_exhaustion_already_reached_leads_the_capacity_risks(captured):
    """days_until_exhaustion 0 is falsy: 'or 999' sorted it last, so the most urgent item fell out of the
    five 'Capacity Risk' bullets."""
    texts, _ = captured
    pdf_report.generate(_data())
    bullets = [t for t in texts if t.startswith('• ') and 'exhaustion' in t]
    assert len(bullets) == 5 and 'Ram' in bullets[0] and 'reached' in bullets[0] and 'imminent' in bullets[0]


def test_the_cluster_wide_row_is_not_counted_as_a_node(captured):
    _, cards = captured
    pdf_report.generate(_data())
    assert ('NODES', '3') in cards
