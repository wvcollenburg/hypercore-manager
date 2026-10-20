"""
reports/pdf_report.py — E5: Foresight PDF report generation.

Manager-ready capacity intelligence report.  Light print theme, narrative
executive summary, curated insights — not raw data dumps.

Contents:
  - Cover page: logo, title, health score badge, cluster metadata
  - Executive Summary: narrative findings, KPI cards, predicted gains
  - Node Overview: per-node CPU + storage current vs 30d forecast
  - Top 10 VMs to Act On: highest-impact right-sizing targets
  - Resource Exhaustion Timeline: alerts ordered by urgency
  - Resize Action Plan: full recommendations with expected outcomes

Dependencies: reportlab, pillow
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sev_color(sev: str | None, accent_hex: str) -> object:
    from reportlab.lib import colors
    mapping = {
        'EMERGENCY': colors.HexColor('#DC2626'),
        'CRITICAL':  colors.HexColor('#DC2626'),
        'WARNING':   colors.HexColor('#D97706'),
        'ATTENTION': colors.HexColor('#7C3AED'),
        'INFO':      colors.HexColor('#16A34A'),
    }
    return mapping.get(sev or 'INFO', colors.HexColor('#16A34A'))


def _short(uuid: str | None, n: int = 8) -> str:
    if not uuid:
        return '—'
    return str(uuid)[:n] + '…'


def _fmt_bytes(val: float | None, dim: str | None) -> str:
    if val is None:
        return '—'
    if dim == 'storage_capacity':
        tb = val / (1024 ** 4)
        if tb >= 1:
            return f'{tb:.2f} TB'
        return f'{val / (1024 ** 3):.0f} GB'
    if dim == 'ram':
        gb = val / (1024 ** 3)
        return f'{gb:.1f} GB'
    if dim == 'network':
        return f'{val / 1e6:.1f} MB/s'
    return f'{val:.1f}'


def _pct(val: float | None) -> str:
    if val is None:
        return '—'
    return f'{val:.1f}%'


def _light_table(story, rows, col_widths, accent_hex, severity_col=None,
                 header_rows=1):
    """Append a print-friendly table: colored header, white/light-gray rows."""
    from reportlab.lib      import colors
    from reportlab.platypus import Table, TableStyle

    tbl = Table(rows, colWidths=col_widths, repeatRows=header_rows)
    h_bg   = colors.HexColor(accent_hex)
    row1   = colors.white
    row2   = colors.HexColor('#F1F5F9')
    border = colors.HexColor('#CBD5E1')
    dark   = colors.HexColor('#1E293B')

    cmds = [
        # header
        ('BACKGROUND',    (0, 0), (-1, header_rows - 1), h_bg),
        ('TEXTCOLOR',     (0, 0), (-1, header_rows - 1), colors.white),
        ('FONTNAME',      (0, 0), (-1, header_rows - 1), 'Helvetica-Bold'),
        # body
        ('ROWBACKGROUNDS', (0, header_rows), (-1, -1), [row1, row2]),
        ('TEXTCOLOR',     (0, header_rows), (-1, -1), dark),
        # all
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('GRID',          (0, 0), (-1, -1), 0.4, border),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]

    if severity_col is not None:
        sev_colors = {
            'EMERGENCY': '#DC2626', 'CRITICAL': '#DC2626',
            'WARNING': '#D97706',   'ATTENTION': '#7C3AED', 'INFO': '#16A34A',
        }
        for i, row in enumerate(rows[header_rows:], start=header_rows):
            cell = str(row[severity_col]) if severity_col < len(row) else ''
            hex_c = sev_colors.get(cell, '#64748B')
            cmds += [
                ('TEXTCOLOR', (severity_col, i), (severity_col, i),
                 colors.HexColor(hex_c)),
                ('FONTNAME',  (severity_col, i), (severity_col, i),
                 'Helvetica-Bold'),
            ]

    tbl.setStyle(TableStyle(cmds))
    story.append(tbl)


def _kpi_card(label: str, value: str, sub: str, accent: str, width: float = 88) -> object:
    """Single KPI card as a 3-row mini-table."""
    from reportlab.lib      import colors
    from reportlab.platypus import Table, TableStyle

    tbl = Table([[label], [value], [sub]], colWidths=[width])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',   (0, 0), (-1, -1), colors.HexColor('#F8FAFC')),
        ('BOX',          (0, 0), (-1, -1), 1.5, colors.HexColor(accent)),
        ('TEXTCOLOR',    (0, 0), (0, 0),   colors.HexColor('#64748B')),
        ('TEXTCOLOR',    (0, 1), (0, 1),   colors.HexColor(accent)),
        ('TEXTCOLOR',    (0, 2), (0, 2),   colors.HexColor('#64748B')),
        ('FONTSIZE',     (0, 0), (0, 0),   7),
        ('FONTSIZE',     (0, 1), (0, 1),   16),
        ('FONTSIZE',     (0, 2), (0, 2),   7),
        ('FONTNAME',     (0, 1), (0, 1),   'Helvetica-Bold'),
        ('ALIGN',        (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',       (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',   (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 4),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# report builder
# ---------------------------------------------------------------------------

def generate(data: dict) -> bytes:
    """
    Build the PDF and return it as bytes.

    `data` must contain:
        cluster_id       str
        generated_at     str  (ISO-8601 UTC)
        health           dict | None
        forecasts        list[dict]
        rightsizing      list[dict]
        resize           list[dict]
        exhaustion       list[dict]

    Optional (E7 branding):
        branding         dict | None   — company_name, accent_color, logo_b64
    """
    import base64

    try:
        from reportlab.lib           import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles    import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units     import mm
        from reportlab.platypus      import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, PageBreak, Image as RLImage, KeepTogether,
        )
    except ImportError:
        log.error('reportlab not installed')
        raise

    # ── branding ──────────────────────────────────────────────────────────────
    branding     = data.get('branding') or {}
    company_name = branding.get('company_name') or ''
    accent_hex   = branding.get('accent_color') or '#3b82f6'
    logo_b64     = branding.get('logo_b64') or ''

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=20*mm,  bottomMargin=20*mm,
    )
    W = A4[0] - 40*mm

    styles = getSampleStyleSheet()
    accent_c  = colors.HexColor(accent_hex)
    dark_c    = colors.HexColor('#1E293B')
    muted_c   = colors.HexColor('#64748B')

    def _h1(text):
        return Paragraph(text, ParagraphStyle(
            'H1', parent=styles['Normal'], fontSize=24, fontName='Helvetica-Bold',
            textColor=accent_c, leading=32, spaceAfter=8))

    def _h2(text):
        return Paragraph(text, ParagraphStyle(
            'H2', parent=styles['Normal'], fontSize=13, fontName='Helvetica-Bold',
            textColor=accent_c, spaceBefore=10, spaceAfter=4))

    def _h3(text):
        return Paragraph(text, ParagraphStyle(
            'H3', parent=styles['Normal'], fontSize=10, fontName='Helvetica-Bold',
            textColor=dark_c, spaceBefore=6, spaceAfter=2))

    def _body(text):
        return Paragraph(text, ParagraphStyle(
            'Body', parent=styles['Normal'], fontSize=9, textColor=dark_c,
            leading=14, spaceAfter=4))

    def _muted(text):
        return Paragraph(text, ParagraphStyle(
            'Muted', parent=styles['Normal'], fontSize=8, textColor=muted_c,
            leading=12))

    def _bullet(text):
        return Paragraph(f'• {text}', ParagraphStyle(
            'Bullet', parent=styles['Normal'], fontSize=9, textColor=dark_c,
            leading=14, leftIndent=12, spaceAfter=3))

    def _hr():
        return HRFlowable(width=W, thickness=0.5,
                          color=colors.HexColor('#CBD5E1'), spaceAfter=6)

    # ── unpack data ────────────────────────────────────────────────────────────
    health      = data.get('health') or {}
    forecasts   = data.get('forecasts') or []
    rightsizing = data.get('rightsizing') or []
    resize      = data.get('resize') or []
    exhaustion  = data.get('exhaustion') or []

    health_score = float(health.get('health_score') or 0)
    severity     = health.get('severity') or 'UNKNOWN'
    bottleneck   = health.get('bottleneck_resource') or '—'
    efficiency   = float(health.get('efficiency_index') or 0)

    over_vms  = [r for r in rightsizing if r.get('sizing_direction') == 'over_provisioned']
    under_vms = [r for r in rightsizing if r.get('sizing_direction') == 'under_provisioned']
    total_reclaim = sum(float(r.get('capacity_reclaim_cpu_pct') or 0) for r in resize)

    # group forecasts by node for overview
    node_data: dict[str, dict] = {}
    for f in forecasts:
        if f.get('forecast_horizon_days') != 30:
            continue
        node = _short(f.get('node_uuid'))
        dim  = f.get('resource_dimension') or ''
        if dim not in ('cpu', 'storage_capacity'):
            continue
        if node not in node_data:
            node_data[node] = {}
        cur  = f.get('current_usage')
        fval = f.get('forecast_value')
        exd  = f.get('exhaustion_date')
        sev  = f.get('severity') or 'INFO'
        node_data[node][dim] = {'cur': cur, 'fval': fval, 'exd': exd, 'sev': sev}

    story = []

    # =========================================================================
    # cover page
    # =========================================================================
    story.append(Spacer(1, 20*mm))

    # logo
    if logo_b64:
        try:
            from PIL import Image as PILImage
            logo_bytes = base64.b64decode(logo_b64)
            logo_buf   = io.BytesIO(logo_bytes)
            with PILImage.open(logo_buf) as pil_img:
                nat_w, nat_h = pil_img.size
            scale    = (28 * mm) / nat_h
            logo_buf.seek(0)
            logo_img = RLImage(logo_buf, width=nat_w * scale, height=28 * mm)
            story.append(logo_img)
            story.append(Spacer(1, 6*mm))
        except Exception:
            log.warning('Branding logo could not be decoded — skipping')

    story.append(_h1('Foresight'))
    story.append(Paragraph('Capacity Intelligence Report', ParagraphStyle(
        'Sub', parent=styles['Normal'], fontSize=14, textColor=dark_c,
        fontName='Helvetica-Bold', spaceBefore=0, spaceAfter=10)))

    if company_name:
        story.append(_body(f'Prepared for: <b>{company_name}</b>'))

    story.append(_muted(f"Cluster: {data.get('cluster_id', '—')}"))
    story.append(_muted(f"Generated: {data.get('generated_at', '—')}"))
    story.append(Spacer(1, 10*mm))

    # health score badge on cover
    sev_hex = {'EMERGENCY': '#DC2626', 'CRITICAL': '#DC2626',
                'WARNING': '#D97706', 'ATTENTION': '#7C3AED', 'INFO': '#16A34A'}.get(severity, '#64748B')

    score_para = Paragraph(
        f'<font size="30"><b>{health_score:.0f}</b></font>',
        ParagraphStyle('BadgeScore', parent=styles['Normal'],
                       textColor=colors.white, alignment=1, leading=36))
    label_para = Paragraph(
        f'<font size="11"><b>/ 100</b></font><br/>'
        f'<font size="13" color="{sev_hex}"><b>{severity}</b></font>',
        ParagraphStyle('BadgeLabel', parent=styles['Normal'],
                       textColor=dark_c, alignment=1, leading=18))

    cover_health = Table(
        [[score_para, label_para]],
        colWidths=[30*mm, 52*mm],
    )
    cover_health.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (0, 0), colors.HexColor(sev_hex)),
        ('BACKGROUND',    (1, 0), (1, 0), colors.HexColor('#F8FAFC')),
        ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('BOX',           (0, 0), (-1, -1), 1.5, colors.HexColor(sev_hex)),
        ('LINEAFTER',     (0, 0), (0, 0),   0.5, colors.HexColor(sev_hex)),
    ]))
    cover_health.hAlign = 'LEFT'
    story.append(cover_health)

    story.append(PageBreak())

    # =========================================================================
    # executive summary
    # =========================================================================
    story.append(_h2('Executive Summary'))
    story.append(_hr())

    # ── KPI cards ─────────────────────────────────────────────────────────────
    vm_count   = len(set(r.get('vm_name') for r in rightsizing))
    node_count = len(node_data) or '?'

    card_w = W / 5 - 8
    kpi_row = Table(
        [[
            _kpi_card('HEALTH SCORE', f'{health_score:.0f}', f'/ 100  {severity}', sev_hex, card_w),
            _kpi_card('NODES', str(node_count), 'in cluster', accent_hex, card_w),
            _kpi_card('VMs FLAGGED', str(len(rightsizing)), 'for right-sizing', accent_hex, card_w),
            _kpi_card('EXHAUSTION ALERTS', str(len(exhaustion)), 'active', '#D97706' if exhaustion else '#16A34A', card_w),
            _kpi_card('EFFICIENCY', f'{efficiency:.0%}', 'cluster avg', '#D97706' if efficiency < 0.5 else '#16A34A', card_w),
        ]],
        colWidths=[W / 5] * 5,
    )
    kpi_row.setStyle(TableStyle([
        ('ALIGN',   (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',  (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(kpi_row)
    story.append(Spacer(1, 6*mm))

    # ── narrative ─────────────────────────────────────────────────────────────
    story.append(_h3('Cluster Health'))

    sev_desc = {
        'EMERGENCY': 'in an emergency state and requires immediate intervention',
        'CRITICAL':  'in critical condition and requires prompt attention',
        'WARNING':   'showing warning signs that should be addressed soon',
        'ATTENTION': 'performing adequately with some items worth monitoring',
        'INFO':      'healthy with no immediate concerns',
    }.get(severity, 'in an unknown state')

    story.append(_body(
        f'The HyperCore cluster is currently <b>{sev_desc}</b>, '
        f'scoring <b>{health_score:.0f} out of 100</b>. '
        f'The primary bottleneck is <b>{bottleneck.replace("_", " ")}</b>. '
        f'Overall resource efficiency stands at <b>{efficiency:.0%}</b>, indicating that a '
        f'{"significant" if efficiency < 0.4 else "moderate"} portion of allocated compute '
        f'capacity is sitting idle.'
    ))

    # exhaustion findings
    if exhaustion:
        story.append(_h3('Capacity Risk'))
        for e in sorted(exhaustion, key=lambda x: x.get('days_until_exhaustion') or 999)[:5]:
            dim  = (e.get('resource_dimension') or '—').replace('_', ' ')
            days = e.get('days_until_exhaustion')
            exd  = e.get('exhaustion_date') or '—'
            sev2 = e.get('severity') or 'INFO'
            node = _short(e.get('node_uuid'))
            urgency = 'is imminent' if (days or 999) <= 30 else 'is approaching'
            story.append(_bullet(
                f'<b>{dim.title()}</b> exhaustion on node {node} {urgency} — '
                f'projected <b>{exd}</b> ({days} days).  Severity: <b>{sev2}</b>.'
            ))
    else:
        story.append(_body('No active resource exhaustion alerts. All dimensions have sufficient runway.'))

    # right-sizing findings
    story.append(_h3('Right-Sizing Opportunity'))
    if over_vms or under_vms:
        if over_vms:
            story.append(_body(
                f'<b>{len(over_vms)} VM(s)</b> are over-provisioned — they hold more vCPU '
                f'allocation than they actually use. These are prime candidates for '
                f'right-sizing to free up cluster headroom.'
            ))
        if under_vms:
            story.append(_body(
                f'<b>{len(under_vms)} VM(s)</b> are under-provisioned and may be '
                f'experiencing CPU contention or performance degradation.'
            ))
        if resize:
            avg_conf = sum(float(r.get('confidence_score') or 0) for r in resize) / len(resize)
            story.append(_body(
                f'Applying all <b>{len(resize)} resize recommendation(s)</b> is projected to '
                f'reclaim approximately <b>{total_reclaim:.1f}% of over-allocated CPU capacity</b> '
                f'across the cluster, with an average recommendation confidence of '
                f'<b>{avg_conf:.0%}</b>.'
            ))
    else:
        story.append(_body('No right-sizing flags are currently active.'))

    # predicted gains table
    if resize:
        story.append(Spacer(1, 4*mm))
        story.append(_h3('Predicted Gains if Recommendations are Applied'))

        low_risk  = [r for r in resize if (r.get('risk_level') or '').upper() == 'LOW']
        med_risk  = [r for r in resize if (r.get('risk_level') or '').upper() == 'MEDIUM']
        high_risk = [r for r in resize if (r.get('risk_level') or '').upper() == 'HIGH']
        low_reclaim  = sum(float(r.get('capacity_reclaim_cpu_pct') or 0) for r in low_risk)
        med_reclaim  = sum(float(r.get('capacity_reclaim_cpu_pct') or 0) for r in med_risk)
        high_reclaim = sum(float(r.get('capacity_reclaim_cpu_pct') or 0) for r in high_risk)

        gains_rows = [['Risk Level', 'VMs', 'Est. CPU Reclaim', 'Recommendation']]
        if low_risk:
            gains_rows.append(['LOW', str(len(low_risk)), f'{low_reclaim:.1f}%',
                                'Apply immediately — low risk of disruption'])
        if med_risk:
            gains_rows.append(['MEDIUM', str(len(med_risk)), f'{med_reclaim:.1f}%',
                                'Apply during next maintenance window'])
        if high_risk:
            gains_rows.append(['HIGH', str(len(high_risk)), f'{high_reclaim:.1f}%',
                                'Review carefully before applying'])
        gains_rows.append(['TOTAL', str(len(resize)), f'{total_reclaim:.1f}%', ''])
        _light_table(story, gains_rows, [25*mm, 18*mm, 35*mm, W - 78*mm], accent_hex)

    story.append(PageBreak())

    # =========================================================================
    # node overview
    # =========================================================================
    story.append(_h2('Node Overview'))
    story.append(_hr())
    story.append(_body(
        'Current and 30-day forecast values for each cluster node. '
        'Nodes approaching capacity thresholds are highlighted.'
    ))
    story.append(Spacer(1, 3*mm))

    if node_data:
        node_rows = [['Node', 'CPU Now', 'CPU (30d)', 'Storage Now', 'Storage (30d)',
                      'Exhaustion', 'Status']]
        for node, dims in sorted(node_data.items()):
            cpu  = dims.get('cpu') or {}
            stor = dims.get('storage_capacity') or {}

            cpu_cur  = f'{float(cpu.get("cur") or 0):.1f}%'  if cpu.get('cur')  is not None else '—'
            cpu_fore = f'{float(cpu.get("fval") or 0):.1f}%' if cpu.get('fval') is not None else '—'
            st_cur   = _fmt_bytes(stor.get('cur'),  'storage_capacity')
            st_fore  = _fmt_bytes(stor.get('fval'), 'storage_capacity')

            worst_sev = 'INFO'
            exd_str   = '—'
            for d in dims.values():
                sev2 = d.get('sev') or 'INFO'
                if {'EMERGENCY': 5, 'CRITICAL': 4, 'WARNING': 3, 'ATTENTION': 2, 'INFO': 1}.get(sev2, 0) > \
                   {'EMERGENCY': 5, 'CRITICAL': 4, 'WARNING': 3, 'ATTENTION': 2, 'INFO': 1}.get(worst_sev, 0):
                    worst_sev = sev2
                if d.get('exd'):
                    exd_str = str(d['exd'])

            node_rows.append([node, cpu_cur, cpu_fore, st_cur, st_fore, exd_str, worst_sev])

        _light_table(story, node_rows,
                     [28*mm, 20*mm, 20*mm, 28*mm, 28*mm, 28*mm, W - 152*mm],
                     accent_hex, severity_col=6)
    else:
        story.append(_muted('No node forecast data available.'))

    story.append(Spacer(1, 8*mm))

    # =========================================================================
    # top 10 VMs to act on
    # =========================================================================
    story.append(_h2('Top 10 VMs to Act On'))
    story.append(_hr())
    story.append(_body(
        'The following virtual machines have the highest right-sizing composite score, '
        'meaning they offer the greatest opportunity for capacity reclaim with the '
        'highest confidence. Addressing these VMs will have the most immediate impact '
        'on cluster health.'
    ))
    story.append(Spacer(1, 3*mm))

    # merge rightsizing + resize for richer rows, capped at 10
    resize_by_vm = {r.get('vm_name'): r for r in resize}
    top10 = sorted(rightsizing, key=lambda r: float(r.get('composite_score') or 0),
                   reverse=True)[:10]

    if top10:
        vm_rows = [['VM', 'Direction', 'Avg CPU', 'P95 CPU', 'Suggested Cut', 'Reclaim', 'Risk', 'Conf.']]
        for r in top10:
            name  = r.get('vm_name') or '—'
            direc = (r.get('sizing_direction') or '—').replace('_', ' ').title()
            avg   = _pct(r.get('cpu_avg_usage_pct'))
            p95   = _pct(r.get('cpu_p95_usage_pct'))
            rz    = resize_by_vm.get(name)
            chg   = f'{float(rz.get("suggested_cpu_change_pct") or 0):+.0%}' if rz else '—'
            rcl   = _pct(rz.get('capacity_reclaim_cpu_pct'))   if rz else '—'
            risk  = (rz.get('risk_level') or '—').upper()      if rz else '—'
            conf  = f'{float(rz.get("confidence_score") or 0):.0%}' if rz else '—'
            vm_rows.append([name, direc, avg, p95, chg, rcl, risk, conf])

        cw = [38*mm, 26*mm, 16*mm, 16*mm, 22*mm, 16*mm, 16*mm, W - 150*mm]
        _light_table(story, vm_rows, cw, accent_hex)
    else:
        story.append(_muted('No right-sizing data available.'))

    story.append(PageBreak())

    # =========================================================================
    # resource exhaustion timeline
    # =========================================================================
    story.append(_h2('Resource Exhaustion Timeline'))
    story.append(_hr())

    if exhaustion:
        story.append(_body(
            'Resources ordered by urgency. Act on CRITICAL items before the exhaustion '
            'date to avoid forced downtime or performance degradation.'
        ))
        story.append(Spacer(1, 3*mm))
        ex_rows = [['Dimension', 'Node', 'Current Usage', 'Growth / day',
                    'Exhaustion Date', 'Days Left', 'Severity']]
        for e in exhaustion[:20]:
            dim   = (e.get('resource_dimension') or '—').replace('_', ' ').title()
            node  = _short(e.get('node_uuid'))
            cur   = _pct(e.get('current_usage_pct'))
            gr    = f'{float(e.get("growth_rate_per_day") or 0):.2f}/d'
            exd   = str(e.get('exhaustion_date') or '—')
            days  = str(e.get('days_until_exhaustion') or '—')
            sev2  = e.get('severity') or 'INFO'
            ex_rows.append([dim, node, cur, gr, exd, days, sev2])
        _light_table(story, ex_rows,
                     [30*mm, 22*mm, 22*mm, 22*mm, 28*mm, 18*mm, W - 142*mm],
                     accent_hex, severity_col=6)
    else:
        story.append(_body(
            'No active resource exhaustion alerts. All monitored dimensions have '
            'sufficient capacity within the forecast horizon.'
        ))

    story.append(Spacer(1, 8*mm))

    # =========================================================================
    # full resize action plan
    # =========================================================================
    story.append(_h2('Full Resize Action Plan'))
    story.append(_hr())

    if resize:
        story.append(_body(
            'Complete list of resize recommendations ordered by expected impact. '
            'CPU Change shows the recommended adjustment as a percentage of current '
            'allocation. Reclaim is the estimated freed capacity as a share of total '
            'cluster CPU.'
        ))
        story.append(Spacer(1, 3*mm))
        rz_rows = [['VM Name', 'Direction', 'CPU Change', 'Target Avg', 'Reclaim', 'Risk', 'Conf.']]
        for r in resize:
            chg = float(r.get('suggested_cpu_change_pct') or 0)
            rz_rows.append([
                r.get('vm_name') or '—',
                (r.get('sizing_direction') or '—').replace('_', ' ').title(),
                f'{chg:+.0%}',
                _pct(r.get('target_cpu_avg_pct')),
                _pct(r.get('capacity_reclaim_cpu_pct')),
                (r.get('risk_level') or '—').upper(),
                f'{float(r.get("confidence_score") or 0):.0%}',
            ])
        cw = [42*mm, 30*mm, 22*mm, 22*mm, 18*mm, 18*mm, W - 152*mm]
        _light_table(story, rz_rows, cw, accent_hex)
    else:
        story.append(_muted('No resize recommendations available.'))

    # ── footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 10*mm))
    story.append(_hr())
    footer_parts = ['Foresight  ·  Capacity Intelligence Module  ·  HyperCore Manager']
    if company_name:
        footer_parts.append(f'Prepared for {company_name}')
    footer_parts.append(f'Generated {data.get("generated_at", "")}')
    story.append(_muted('  ·  '.join(footer_parts)))

    doc.build(story)
    return buf.getvalue()
