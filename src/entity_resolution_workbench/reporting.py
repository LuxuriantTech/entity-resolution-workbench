from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, cast

from .common import canonical_bytes

NORMALIZED_FIELDS = (
    ("name", "Name"),
    ("brand", "Brand"),
    ("sku", "SKU"),
    ("category", "Category"),
    ("price", "Price"),
)
SCORE_COMPONENTS = (
    ("name", "Name"),
    ("brand", "Brand"),
    ("sku", "SKU"),
    ("price", "Price"),
    ("total", "Total"),
)


@dataclass(frozen=True)
class RenderedReport:
    json_bytes: bytes
    html_bytes: bytes


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _human_value(value: object, *, missing: str = "Not available") -> str:
    return missing if value is None or value == "" else _escape(value)


def _human_list(value: object, *, empty: str = "None") -> str:
    if not isinstance(value, list):
        return _human_value(value, missing=empty)
    return empty if not value else ", ".join(_escape(item) for item in value)


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _metric_rows(payload: dict[str, Any]) -> str:
    metrics = payload.get("metrics_by_split")
    if not isinstance(metrics, dict):
        return ""
    rows = []
    for split in sorted(metrics):
        values = metrics[split]
        if not isinstance(values, dict):
            continue
        cells = "".join(
            f"<td>{_escape(values.get(key))}</td>"
            for key in ("tp", "fp", "tn", "fn", "precision", "recall", "f1")
        )
        rows.append(f'<tr><th scope="row">{_escape(split)}</th>{cells}</tr>')
    return "".join(rows)


def _normalization_table(side: str, values: object) -> str:
    normalized = _mapping(values)
    rows = "".join(
        f'<tr><th scope="row">{label}</th><td>{_human_value(normalized.get(key))}</td></tr>'
        for key, label in NORMALIZED_FIELDS
    )
    return (
        '<table class="detail-table normalized-table">'
        f"<caption>Normalized {side} attributes</caption>"
        '<thead><tr><th scope="col">Attribute</th><th scope="col">Value</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _score_table(scores: object) -> str:
    score_values = _mapping(scores)
    rows = []
    for key, label in SCORE_COMPONENTS:
        values = _mapping(score_values.get(key))
        rows.append(
            '<tr><th scope="row">'
            f"{label}</th><td>{_human_value(values.get('display'))}</td>"
            f"<td>{_human_value(values.get('fraction'))}</td></tr>"
        )
    return (
        '<table class="detail-table score-table">'
        "<caption>Score components</caption>"
        '<thead><tr><th scope="col">Component</th><th scope="col">Display</th>'
        '<th scope="col">Exact fraction</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _review_cards(payload: dict[str, Any]) -> str:
    reviews = payload.get("review_rows")
    if not isinstance(reviews, list):
        return ""
    if not reviews:
        return '<li class="review-empty">No pairs require human review.</li>'
    cards = []
    for index, review in enumerate(reviews, start=1):
        if not isinstance(review, dict):
            continue
        scores = _mapping(review.get("scores"))
        total = _mapping(scores.get("total"))
        total_display = total.get("display")
        total_fraction = total.get("fraction")
        explanation = _mapping(review.get("explanation"))
        normalization = _mapping(explanation.get("normalization"))
        heading_id = f"review-pair-{index}"
        cards.append(
            '<li><article class="review-card" data-decision="'
            f'{_escape(review.get("decision", ""))}" aria-labelledby="{heading_id}">'
            f'<h3 id="{heading_id}">Review pair {index}</h3>'
            '<p class="pair-identifiers"><strong>Left:</strong> '
            f"<code>{_human_value(review.get('left_id'))}</code> "
            '<span aria-hidden="true">↔</span> <strong>Right:</strong> '
            f"<code>{_human_value(review.get('right_id'))}</code></p>"
            '<dl class="review-summary">'
            f"<div><dt>Split</dt><dd>{_human_value(review.get('split'))}</dd></div>"
            f"<div><dt>Decision</dt><dd>{_human_value(review.get('decision'))}</dd></div>"
            "<div><dt>Total score</dt><dd>"
            f"{_human_value(total_display)} ({_human_value(total_fraction)})</dd></div>"
            "<div><dt>Evidence components</dt><dd>"
            f"{_human_value(review.get('evidence_count'))}</dd></div>"
            "<div><dt>Left rank / margin</dt><dd>"
            f"{_human_value(explanation.get('left_rank'))} / "
            f"{_human_value(explanation.get('left_margin'))}</dd></div>"
            "<div><dt>Right rank / margin</dt><dd>"
            f"{_human_value(explanation.get('right_rank'))} / "
            f"{_human_value(explanation.get('right_margin'))}</dd></div>"
            "</dl>"
            '<div class="review-grid">'
            f"{_normalization_table('left', normalization.get('left'))}"
            f"{_normalization_table('right', normalization.get('right'))}"
            f"{_score_table(scores)}"
            '<section class="review-rationale"><h4>Why human review is required</h4><dl>'
            "<div><dt>Failed MATCH conditions</dt><dd>"
            f"{_human_list(explanation.get('failed_conditions'))}</dd></div>"
            "<div><dt>Contradictions</dt><dd>"
            f"{_human_list(review.get('contradictions'))}</dd></div>"
            "<div><dt>Blocking evidence</dt><dd>"
            f"{_human_list(review.get('block_reasons'))}</dd></div>"
            "<div><dt>Missing components</dt><dd>"
            f"{_human_list(explanation.get('missing_components'))}</dd></div>"
            "<div><dt>Other reasons</dt><dd>"
            f"{_human_list(explanation.get('reasons'))}</dd></div>"
            "</dl></section></div></article></li>"
        )
    return "".join(cards)


def render_report(payload: dict[str, object]) -> RenderedReport:
    json_bytes = canonical_bytes(payload)
    metrics = _metric_rows(payload)
    reviews = _review_cards(payload)
    canonical_json = html.escape(json_bytes.decode("utf-8"), quote=True)
    document = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Entity Resolution Workbench report</title>"
        "<style>*{box-sizing:border-box}body{font:15px system-ui,sans-serif;max-width:1100px;"
        "margin:0 auto;padding:1.5rem;overflow-wrap:anywhere;color:#1c2430;background:#fff}"
        "h1,h2,h3,h4{line-height:1.2}table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem}"
        "caption{text-align:left;font-weight:700;padding:.35rem 0}th,td{border:1px solid #aeb6c2;"
        "padding:.45rem;text-align:left;vertical-align:top}thead{background:#edf1f5}"
        "code,pre{font-family:ui-monospace,monospace;overflow-wrap:anywhere;word-break:break-word}"
        "pre{white-space:pre-wrap}.scroll-hint{margin-bottom:.35rem}"
        ".table-scroll{max-width:100%;overflow-x:auto;overscroll-behavior-inline:contain;"
        "border:2px solid #52677f;padding:.25rem}"
        ".table-scroll:focus{outline:3px solid #0b6ecf;outline-offset:2px}"
        ".metrics-table{min-width:42rem;margin:0}"
        ".review-list{list-style:none;margin:1rem 0;padding:0;display:grid;gap:1rem}"
        ".review-card{border:2px solid #52677f;border-radius:.35rem;padding:1rem;"
        "min-width:0;max-width:100%}.pair-identifiers{line-height:1.6}.review-summary{display:grid;"
        "grid-template-columns:repeat(auto-fit,minmax(min(100%,12rem),1fr));gap:.5rem;"
        "margin:1rem 0}"
        ".review-summary div,.review-rationale dl div{background:#f4f6f8;padding:.5rem;min-width:0}"
        "dt{font-weight:700}dd{margin:.2rem 0 0}.review-grid{display:grid;"
        "grid-template-columns:repeat(auto-fit,minmax(min(100%,18rem),1fr));gap:.75rem;align-items:start}"
        ".detail-table{table-layout:fixed;min-width:0}.detail-table th:first-child{width:38%}"
        ".detail-table th,.detail-table td{overflow-wrap:anywhere;word-break:break-word}"
        ".review-rationale{min-width:0}.review-rationale dl{display:grid;gap:.45rem;margin:.5rem 0}"
        "@media(max-width:480px){body{font-size:14px;padding:.75rem}"
        ".review-card{padding:.7rem}.review-summary{grid-template-columns:1fr}"
        ".detail-table th,.detail-table td{padding:.35rem}}"
        "</style></head><body>"
        "<h1>Entity Resolution Workbench</h1>"
        f"<p>Run <code>{_escape(payload.get('run_key', ''))}</code>; evaluation "
        f"<code>{_escape(payload.get('evaluation_key', ''))}</code>.</p>"
        '<h2 id="metrics-heading">Metrics by frozen split</h2>'
        '<p id="metrics-scroll-help" class="scroll-hint">On narrow screens, scroll the metrics '
        "table horizontally to reach every column.</p>"
        '<div class="table-scroll" role="region" tabindex="0" aria-labelledby="metrics-heading" '
        'aria-describedby="metrics-scroll-help"><table class="metrics-table">'
        "<caption>Confusion matrix and descriptive metrics for each frozen split</caption>"
        '<thead><tr><th scope="col">Split</th><th scope="col">TP</th>'
        '<th scope="col">FP</th><th scope="col">TN</th><th scope="col">FN</th>'
        '<th scope="col">Precision</th><th scope="col">Recall</th><th scope="col">F1</th>'
        f"</tr></thead><tbody>{metrics}</tbody></table></div>"
        '<section aria-labelledby="review-queue-heading"><h2 id="review-queue-heading">'
        "Human-review queue</h2>"
        "<p>Each REVIEW card exposes the normalized pair, decomposed exact scores, margins, "
        "contradictions, and failed automatic-match conditions.</p>"
        f'<ol class="review-list">{reviews}</ol></section>'
        f"<details><summary>Canonical JSON</summary><pre>{canonical_json}</pre></details>"
        "</body></html>\n"
    )
    return RenderedReport(json_bytes=json_bytes, html_bytes=document.encode("utf-8"))
