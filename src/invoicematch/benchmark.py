from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from statistics import median
from time import perf_counter, sleep
from typing import Literal

from pydantic import ValidationError

from invoicematch.domain import (
    Contract,
    Finding,
    InvoiceExtraction,
    InvoiceLine,
    Money,
    OrderLine,
    PurchaseOrder,
    Reconciliation,
    Verdict,
)
from invoicematch.evidence import source_from_pages
from invoicematch.model import (
    MODEL_ID,
    PARAMETERS,
    PROMPT_VERSION,
    Invocation,
    ModelFailure,
    invoke_groq,
    load_groq_key,
    normalize_model,
    protocol_hash,
)
from invoicematch.pdf import read_pdf_pages
from invoicematch.reconciliation import extract_rules, reconcile

FIXTURES = Path("tests/fixtures/phase2")
REFERENCE_ORDER = PurchaseOrder(
    order_id="PO-001",
    supplier_id="SUP-001",
    lines=(
        OrderLine(item_id="A-10", quantity=10, unit_price=Decimal("100.00")),
        OrderLine(item_id="B-20", quantity=2, unit_price=Decimal("50.00")),
    ),
)
Mode = Literal["rules", "groq", "replay"]
FieldValue = str | int | Decimal
Infer = Callable[[tuple[str, ...]], Invocation]


class GoldLine(OrderLine):
    total: Money


class GoldCase(Contract):
    case_id: str
    layout: Literal["compact", "wrapped"]
    sha256: str
    expected: Literal["match", "mismatch"]
    invoice_number: str
    supplier_id: str
    currency: str
    total: Money
    lines: tuple[GoldLine, ...]


class _Manifest(Contract):
    dataset: Literal["phase2-heldout-v1"]
    cases: tuple[GoldCase, ...]


class Observation(Contract):
    verdict: Verdict
    technical_error: str | None = None
    elapsed_ms: float
    exact_fields: dict[str, bool]
    extra_rows: int
    findings: tuple[Finding, ...]


class CaseResult(Contract):
    case_id: str
    layout: str
    expected: Literal["match", "mismatch"]
    rules: Observation
    model: Observation | None
    invocation: Invocation | None


class Report(Contract):
    mode: Mode
    source_revision: str
    generated_at: datetime
    manifest_sha256: str
    protocol_sha256: str
    requested_model: str
    prompt_version: str
    parameters: dict[str, str | int]
    interval_seconds: float | None
    cases: tuple[CaseResult, ...]


def _load_dataset(folder: Path) -> tuple[str, list[tuple[GoldCase, tuple[str, ...]]]]:
    manifest_bytes = (folder / "labels.json").read_bytes()
    # Git checkout may translate textual line endings. Hash the canonical JSON instead.
    manifest = _Manifest.model_validate_json(manifest_bytes)
    ids = [case.case_id for case in manifest.cases]
    if ids != [f"H{number:02}" for number in range(1, 13)]:
        raise ValueError("The held-out manifest must contain H01 through H12 in order.")
    groups = Counter((case.layout, case.expected) for case in manifest.cases)
    if len(groups) != 4 or set(groups.values()) != {3}:
        raise ValueError("Each layout must contain three valid and three discrepant invoices.")
    cases = []
    for case in manifest.cases:
        data = (folder / f"{case.case_id}.pdf").read_bytes()
        if sha256(data).hexdigest() != case.sha256:
            raise ValueError(f"PDF hash differs for {case.case_id}.")
        cases.append((case, read_pdf_pages(data)))
    canonical = json.dumps(json.loads(manifest_bytes), sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest(), cases


def _fields_for_row(row: GoldLine | InvoiceLine, prefix: str) -> dict[str, FieldValue]:
    return {
        f"{prefix}.item_id": row.item_id,
        f"{prefix}.quantity": row.quantity,
        f"{prefix}.unit_price": row.unit_price,
        f"{prefix}.total": row.total,
    }


def _observe(
    case: GoldCase,
    extraction: InvoiceExtraction,
    result: Reconciliation,
    elapsed_ms: float,
    error: str | None,
) -> Observation:
    gold: dict[str, FieldValue] = {
        "invoice_number": case.invoice_number,
        "supplier_id": case.supplier_id,
        "currency": case.currency,
        "total": case.total,
    }
    predicted: dict[str, FieldValue | None] = {
        name: claim.value if claim else None
        for name, claim in (
            ("invoice_number", extraction.invoice_number),
            ("supplier_id", extraction.supplier_id),
            ("currency", extraction.currency),
            ("total", extraction.total),
        )
    }
    for row in case.lines:
        gold.update(_fields_for_row(row, f"line.{row.item_id}"))
    counts = Counter(row.item_id for row in extraction.lines)
    for predicted_row in extraction.lines:
        if counts[predicted_row.item_id] == 1:
            predicted.update(_fields_for_row(predicted_row, f"line.{predicted_row.item_id}"))
    extra = sum((counts - Counter(row.item_id for row in case.lines)).values())
    return Observation(
        verdict=result.verdict,
        technical_error=error,
        elapsed_ms=round(elapsed_ms, 4),
        exact_fields={key: predicted.get(key) == value for key, value in gold.items()},
        extra_rows=extra,
        findings=result.findings,
    )


def _score(cases: tuple[CaseResult, ...], arm: Literal["rules", "model"]) -> dict[str, object]:
    runs = [(case, case.rules if arm == "rules" else case.model) for case in cases]
    performance = [(case, run) for case, run in runs if run is not None]
    valid = [(case, run) for case, run in performance if case.expected == "match"]
    bad = [(case, run) for case, run in performance if case.expected == "mismatch"]
    field_counts: dict[str, list[int]] = {}
    for _, run in performance:
        for key, exact in run.exact_fields.items():
            kind = "line." + key.rsplit(".", 1)[1] if key.startswith("line.") else key
            counts = field_counts.setdefault(kind, [0, 0])
            counts[0] += int(exact)
            counts[1] += 1
    return {
        "cases": len(performance),
        "false_matches": [sum(run.verdict == "match" for _, run in bad), len(bad)],
        "valid_non_matches": [sum(run.verdict != "match" for _, run in valid), len(valid)],
        "valid_mismatch": sum(run.verdict == "mismatch" for _, run in valid),
        "valid_needs_review": sum(run.verdict == "needs_review" for _, run in valid),
        "needs_review": sum(run.verdict == "needs_review" for _, run in performance),
        "technical_failures": sum(run.technical_error is not None for _, run in performance),
        "exact_fields": [
            sum(row[0] for row in field_counts.values()),
            sum(row[1] for row in field_counts.values()),
        ],
        "by_field": field_counts,
        "extra_rows": sum(run.extra_rows for _, run in performance),
    }


def summarize(report: Report) -> dict[str, object]:
    rules = _score(report.cases, "rules")
    times = [case.rules.elapsed_ms for case in report.cases]
    summary: dict[str, object] = {
        "mode": report.mode,
        "rules": rules,
        "rules_latency_ms_median": median(times),
    }
    if report.mode == "rules":
        return summary
    model = _score(report.cases, "model")
    summary["model"] = model
    summary["decision_disagreements"] = [
        case.case_id
        for case in report.cases
        if case.model and case.model.verdict != case.rules.verdict
    ]
    if report.mode == "groq":
        model_runs = [case.model for case in report.cases if case.model is not None]
        latency = sorted(run.elapsed_ms for run in model_runs)
        usages = [
            case.invocation.usage
            for case in report.cases
            if case.invocation and case.invocation.usage
        ]
        cost = sum((usage.list_price_usd() for usage in usages), Decimal(0))
        summary["model_latency_ms_median"] = median(latency) if latency else None
        summary["model_latency_ms_p95_nearest_rank"] = (
            latency[math.ceil(len(latency) * 0.95) - 1] if latency else None
        )
        summary["input_tokens"] = sum(usage.prompt_tokens for usage in usages)
        summary["output_tokens"] = sum(usage.completion_tokens for usage in usages)
        summary["estimated_cost_usd"] = str(cost) if usages else None
        summary["calls_with_usage"] = len(usages)
        summary["usage_complete"] = len(usages) == len(report.cases) == 12
        summary["prefer_model"] = (
            len(model_runs) == len(report.cases) == 12
            and all(not run.technical_error for run in model_runs)
            and all(not case.rules.technical_error for case in report.cases)
            and sum(
                case.model is not None
                and case.model.verdict == "match"
                and case.expected == "mismatch"
                for case in report.cases
            )
            < sum(
                case.rules.verdict == "match" and case.expected == "mismatch"
                for case in report.cases
            )
            and sum(
                case.model is not None
                and case.model.verdict != "match"
                and case.expected == "match"
                for case in report.cases
            )
            <= sum(
                case.rules.verdict != "match" and case.expected == "match" for case in report.cases
            )
        )
    return summary


def _same_outcome(current: Observation, previous: Observation) -> bool:
    return current.model_dump(exclude={"elapsed_ms"}) == previous.model_dump(exclude={"elapsed_ms"})


def run_benchmark(
    mode: Mode,
    revision: str,
    *,
    folder: Path = FIXTURES,
    infer: Infer | None = None,
    capture: Report | None = None,
    interval_seconds: float = 10,
) -> Report:
    digest, pages_by_case = _load_dataset(folder)
    if mode == "groq" and infer is None:
        raise ValueError("Live mode requires a configured inference function.")
    if mode == "replay" and (
        capture is None
        or capture.mode != "groq"
        or capture.manifest_sha256 != digest
        or capture.protocol_sha256 != protocol_hash()
        or [entry.case_id for entry in capture.cases] != [case.case_id for case, _ in pages_by_case]
    ):
        raise ValueError("Replay requires a matching live capture and the frozen dataset.")

    case_results = []
    for index, (case, pages) in enumerate(pages_by_case):
        source = source_from_pages(pages)
        start = perf_counter()
        rules = extract_rules(source)
        rule_result = reconcile(REFERENCE_ORDER, source, rules)
        observed_rules = _observe(case, rules, rule_result, (perf_counter() - start) * 1000, None)
        invocation = None
        observed_model = None
        recorded = capture.cases[index] if mode == "replay" and capture else None
        if mode != "rules":
            if mode == "groq" and index:
                sleep(interval_seconds)
            start = perf_counter()
            invocation = recorded.invocation if recorded else infer(pages) if infer else None
            if invocation is None:
                raise ValueError(f"Missing captured response for {case.case_id}.")
            if mode == "groq" and invocation.error in {
                "groq_http_400",
                "groq_http_401",
                "groq_http_403",
                "groq_http_404",
            }:
                raise ModelFailure(invocation.error)
            error = invocation.error
            model_extraction = InvoiceExtraction()
            if invocation.claims is not None and error is None:
                try:
                    model_extraction = normalize_model(invocation.claims, source)
                except ModelFailure as failure:
                    error = failure.code
            if error is None and invocation.claims is None:
                error = "missing_model_claims"
            model_result = (
                Reconciliation(
                    verdict="needs_review",
                    findings=(Finding(code=error, field="extraction"),),
                )
                if error
                else reconcile(REFERENCE_ORDER, source, model_extraction)
            )
            observed_model = _observe(
                case, model_extraction, model_result, (perf_counter() - start) * 1000, error
            )
            if recorded and (
                recorded.model is None
                or not _same_outcome(observed_model, recorded.model)
                or not _same_outcome(observed_rules, recorded.rules)
            ):
                raise ValueError(f"Replay outcome changed for {case.case_id}.")
        case_results.append(
            CaseResult(
                case_id=case.case_id,
                layout=case.layout,
                expected=case.expected,
                rules=observed_rules,
                model=observed_model,
                invocation=invocation,
            )
        )
    return Report(
        mode=mode,
        source_revision=revision,
        generated_at=datetime.now(UTC),
        manifest_sha256=digest,
        protocol_sha256=protocol_hash(),
        requested_model=MODEL_ID,
        prompt_version=PROMPT_VERSION,
        parameters=PARAMETERS,
        interval_seconds=interval_seconds if mode == "groq" else None,
        cases=tuple(case_results),
    )


def _revision(mode: Mode) -> str:
    if (
        mode == "groq"
        and subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], check=False).returncode != 0
    ):
        raise ValueError("Commit the benchmark code before collecting live measurements.")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate InvoiceMatch on the frozen synthetic dataset."
    )
    parser.add_argument("--mode", choices=("rules", "groq", "replay"), default="rules")
    parser.add_argument(
        "--capture", type=Path, help="Previously recorded live report for offline replay"
    )
    parser.add_argument("--output", type=Path, help="Write the per-case JSON report")
    parser.add_argument("--interval-seconds", type=float, default=10)
    args = parser.parse_args()
    mode: Mode = args.mode
    if not math.isfinite(args.interval_seconds) or not 0 <= args.interval_seconds <= 60:
        parser.error("--interval-seconds must be between 0 and 60.")
    if (mode == "replay") != (args.capture is not None):
        parser.error("Use --capture only with --mode replay.")
    if mode == "groq" and args.output is None:
        parser.error("Use --output to record the live run.")
    if args.capture is not None and args.output and args.capture.resolve() == args.output.resolve():
        parser.error("Use distinct capture and replay output paths.")
    try:
        capture = Report.model_validate_json(args.capture.read_bytes()) if args.capture else None
        infer = None
        if mode == "groq":
            key = load_groq_key()

            def infer(pages: tuple[str, ...]) -> Invocation:
                return invoke_groq(pages, key)

        report = run_benchmark(
            mode,
            _revision(mode),
            infer=infer,
            capture=capture,
            interval_seconds=args.interval_seconds,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        print(json.dumps(summarize(report), indent=2))
    except (
        ModelFailure,
        OSError,
        ValueError,
        ValidationError,
        subprocess.CalledProcessError,
    ) as error:
        message = error.code if isinstance(error, ModelFailure) else type(error).__name__
        parser.exit(1, f"Benchmark failed ({message}).\n")


if __name__ == "__main__":
    main()
