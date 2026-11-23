import json
from decimal import Decimal
from pathlib import Path

import pytest

from invoicematch import model
from invoicematch.benchmark import FIXTURES, REFERENCE_ORDER, run_benchmark, summarize
from invoicematch.evidence import source_from_pages
from invoicematch.model import Invocation, ModelClaims, ModelFailure, invoke_groq, normalize_model
from invoicematch.pdf import read_pdf_pages
from invoicematch.reconciliation import reconcile


def _pages(case: str = "H01") -> tuple[str, ...]:
    return read_pdf_pages((FIXTURES / f"{case}.pdf").read_bytes())


def _simulated_claims(pages: tuple[str, ...]) -> ModelClaims:
    source = source_from_pages(pages)
    return ModelClaims.model_validate(
        {
            field.name: {
                "reference": f"{field.evidence.page}:{field.evidence.line}",
                "value": field.value,
            }
            for field in source.headers
        }
        | {
            "lines": [
                {
                    "reference": f"{row.evidence.page}:{row.evidence.line}",
                    "item_id": row.item_id,
                    "quantity": int(row.quantity),
                    "unit_price": row.unit_price,
                    "total": row.total,
                }
                for row in source.rows
            ]
        }
    )


def test_groq_response_round_trips_without_sending_purchase_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = _pages()

    def fake_request(payload: bytes, api_key: str) -> bytes:
        config = json.loads(payload)
        assert api_key == "synthetic-test-key"
        assert config["response_format"]["json_schema"]["strict"] is True
        assert config["model"] == model.MODEL_ID
        assert "PO-001" not in config["messages"][1]["content"]
        assert "Related B-20" not in config["messages"][1]["content"]
        return json.dumps(
            {
                "model": model.MODEL_ID,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": _simulated_claims(pages).model_dump_json()},
                    }
                ],
                "system_fingerprint": "synthetic-fingerprint",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 50},
                },
            }
        ).encode()

    monkeypatch.setattr(model, "_request", fake_request)
    received = invoke_groq(pages, "synthetic-test-key")
    assert received.error is None
    assert received.claims is not None
    assert received.usage is not None
    assert received.usage.list_price_usd() == Decimal("0.0000206")
    source = source_from_pages(pages)
    assert (
        reconcile(REFERENCE_ORDER, source, normalize_model(received.claims, source)).verdict
        == "match"
    )


def test_api_timeout_is_a_provider_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*args: object, **kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(model, "HTTPSConnection", timed_out)
    assert invoke_groq(_pages(), "synthetic-test-key").error == "provider_timeout"


def test_schema_rejection_differs_from_invalid_request() -> None:
    response = b'{"error":{"code":"json_validate_failed","failed_generation":"untrusted text"}}'
    assert model._provider_error_code(400, response) == "provider_schema_failure"
    assert model._provider_error_code(400, b'{"error":{"code":"bad_schema"}}') == "groq_http_400"
    assert model._provider_error_code(429, response) == "groq_http_429"


@pytest.mark.parametrize(
    ("message", "finish_reason", "code"),
    [
        ("malformed", "stop", "invalid_model_claims"),
        (None, "length", "incomplete_model_response"),
    ],
)
def test_bad_provider_output_keeps_usage_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, message: str | None, finish_reason: str, code: str
) -> None:
    envelope = {
        "model": model.MODEL_ID,
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "choices": [{"finish_reason": finish_reason, "message": {"content": message}}],
    }
    monkeypatch.setattr(model, "_request", lambda *args: json.dumps(envelope).encode())
    result = invoke_groq(_pages(), "synthetic-test-key")
    assert result.error == code
    assert result.claims is None
    assert result.usage is not None


def test_model_cannot_pass_with_a_neighboring_rows_price() -> None:
    pages = _pages("H03")
    source = source_from_pages(pages)
    claims = _simulated_claims(pages)
    a, b = claims.lines
    claims = claims.model_copy(
        update={"lines": (a.model_copy(update={"unit_price": b.unit_price}), b)}
    )
    result = reconcile(REFERENCE_ORDER, source, normalize_model(claims, source))
    assert result.verdict == "needs_review"
    assert "unverified_line" in {finding.code for finding in result.findings}
    claims = claims.model_copy(update={"lines": (a.model_copy(update={"reference": "9:999"}), b)})
    with pytest.raises(ModelFailure, match="unknown_evidence_reference"):
        normalize_model(claims, source)


def test_paired_replay_reproduces_provider_failure_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def simulated(pages: tuple[str, ...]) -> Invocation:
        if pages == _pages():
            return Invocation(claims=None, error="groq_http_429")
        return Invocation(claims=_simulated_claims(pages), error=None)

    capture = run_benchmark("groq", "synthetic", infer=simulated, interval_seconds=0)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    replay = run_benchmark("replay", "synthetic", capture=capture)
    assert summarize(replay)["decision_disagreements"] == ["H01"]
    assert replay.cases[0].model is not None
    assert replay.cases[0].model.verdict == "needs_review"
    assert replay.cases[0].model.technical_error == "groq_http_429"
    assert "estimated_cost_usd" not in summarize(replay)
    assert summarize(capture)["prefer_model"] is False
    wrong = capture.model_copy(update={"protocol_sha256": "changed"})
    with pytest.raises(ValueError, match="matching live capture"):
        run_benchmark("replay", "synthetic", capture=wrong)


def test_frozen_baseline_denominators() -> None:
    summary = summarize(run_benchmark("rules", "synthetic"))
    rules = summary["rules"]
    assert isinstance(rules, dict)
    assert rules["false_matches"] == [0, 6]
    assert rules["valid_non_matches"] == [0, 6]
    assert rules["exact_fields"] == [140, 140]


def test_load_groq_key_from_private_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text('GROQ_API_KEY="synthetic-test-key"\n', encoding="utf-8")
    assert model.load_groq_key(path) == "synthetic-test-key"
    path.write_text("GROQ_API_KEY=a\nGROQ_API_KEY=b\n", encoding="utf-8")
    with pytest.raises(ModelFailure, match="credentials_missing_or_ambiguous"):
        model.load_groq_key(path)
