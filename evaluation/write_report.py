"""Write current evaluation results as machine-readable JSON and Markdown."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

from evaluation.run_evaluation import run_evaluation


REPORT_DIR = Path(__file__).resolve().parent / "reports"


def render_markdown(report: dict, evaluated_commit: str, evaluated_date: str) -> str:
    metrics = report["metrics"]
    scenario_ids = [case["scenario_id"] for case in metrics["cases"]]
    failed = [
        case for case in metrics["cases"]
        if case["expected_status"] != case["predicted_status"]
        or case["expected_outcome"] != case["predicted_outcome"]
        or case["expected_invoice_id"] != case["predicted_invoice_id"]
        or case["expected_anomalies"] != case["predicted_anomalies"]
    ]

    def rate(value: dict) -> str:
        percentage = "n/a" if value["percent"] is None else f"{value['percent']:.2f}%"
        return f"{value['numerator']}/{value['denominator']} ({percentage})"

    lines = [
        "# FinAudit AI Evaluation Report",
        "",
        f"- Evaluated date: {evaluated_date}",
        f"- Evaluated commit: `{evaluated_commit}`",
        f"- Dataset: {metrics['case_count']} hand-curated cases; temporary SQLite database",
        f"- Scenario IDs: {', '.join(scenario_ids)}",
        "- Labels: hand-curated, not independently reviewed; reviewer status remains pending.",
        "- Scope: synthetic smoke/regression examples, not production performance evidence.",
        "",
        "## Metrics",
        "",
        "| Metric | Counts | Rate |",
        "| --- | ---: | ---: |",
        f"| Invoice match precision | TP {metrics['match']['true_positive']}, FP {metrics['match']['false_positive']} | {rate(metrics['match']['precision'])} |",
        f"| Invoice match recall | TP {metrics['match']['true_positive']}, FN {metrics['match']['false_negative']} | {rate(metrics['match']['recall'])} |",
        f"| Invoice match true negatives | TN | {metrics['match']['true_negative']} cases |",
        f"| Anomaly precision | TP {metrics['anomalies']['true_positive']}, FP {metrics['anomalies']['false_positive']} | {rate(metrics['anomalies']['precision'])} |",
        f"| Anomaly recall | TP {metrics['anomalies']['true_positive']}, FN {metrics['anomalies']['false_negative']} | {rate(metrics['anomalies']['recall'])} |",
        f"| False auto-approvals | {metrics['false_auto_approval']['count']} / {metrics['false_auto_approval']['auto_approval_count']} auto-approvals | {rate(metrics['false_auto_approval']['among_auto_approvals'])} |",
        f"| Status accuracy | exact cases | {rate(metrics['status_accuracy'])} |",
        f"| Outcome accuracy | exact cases | {rate(metrics['outcome_accuracy'])} |",
        f"| Manual review | {metrics['manual_review']['pending_count']} pending | {rate(metrics['manual_review']['rate'])} |",
        f"| Processing failures | {metrics['processing_failures']['count']} | {rate(metrics['processing_failures']['rate'])} |",
        "",
        "A zero denominator is shown as `n/a` (JSON uses `null`). Invoice match compares exact invoice IDs; wrong assignments count as FP and FN. Anomaly metrics count `(transaction_id, anomaly_type)` pairs.",
        "",
        f"Malformed CSV check: HTTP {report['csv_validation']['http_status']}, {report['csv_validation']['rejected_row_count']} labelled rows rejected; saved results remained unchanged.",
        "",
        "## Scenario discrepancies",
        "",
    ]
    if failed:
        lines.extend(f"- `{case['scenario_id']}`: expected {case['expected_outcome']}, predicted {case['predicted_outcome']}." for case in failed)
    else:
        lines.append("No discrepancies in the labelled cases.")
    lines.extend(["", "## Limitations", "", report["dataset_limitations"], ""])
    return "\n".join(lines)


def write_reports() -> tuple[Path, Path]:
    report = run_evaluation()
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    evaluated_date = date.today().isoformat()
    payload = {**report, "evaluated_date": evaluated_date, "evaluated_commit": commit}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / "latest.json"
    markdown_path = REPORT_DIR / "latest.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(payload, commit, evaluated_date), encoding="utf-8")
    return json_path, markdown_path


if __name__ == "__main__":
    json_path, markdown_path = write_reports()
    print(f"Wrote {json_path} and {markdown_path} from current evaluator output.")
