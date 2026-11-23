# InvoiceMatch repository instructions

## Commands

The local rules-based reviewer and Phase 2 benchmark have verification commands in the README. See the [local workflow](README.md#run-the-local-reviewer) and [paired benchmark](README.md#paired-extraction-benchmark).

| Task | Command |
| --- | --- |
| Setup | Create a Python 3.11+ virtual environment, activate it, and run `python -m pip install -e ".[dev]"`. |
| Run | `docker compose up --build` |
| Test | `python -m pytest` |
| Lint and format check | `python -m ruff check .` and `python -m ruff format --check .` |
| Type-check | `python -m mypy src` |
| Build | `docker compose build` |
| Replay the benchmark | `python -m invoicematch.benchmark --mode replay --capture benchmarks/phase2-groq.json` |

## Repository layout

- [README.md](README.md) records the product, phase gates, and Phase 0 design.
- `src/invoicematch/` contains the HTTP entry point, bounded workers, extraction, reconciliation, storage, browser assets, and demo PDFs with replay data.
- `tests/` contains unit and HTTP integration checks. `tests/fixtures/phase2/` holds the frozen benchmark PDFs and their labels.
- `benchmarks/` holds the live capture for reproducibility.
- `.github/workflows/` runs tests, lint, type checking, offline replay, and the container build.

## Conventions

- Follow the [phase gates](README.md#phases). Ask for approval before changing the schema, introducing a dependency or public interface, or editing more than about three files.
- Ask the independent Scrutiny agent to inspect the exact candidate revision at each gate. Record its findings and rerun affected checks after fixes. Advance after the gate passes.
- Use typed public Python functions, explicit errors, exact decimal money, and parameterized database operations. Exercise behavior through its normal entry point. Use synthetic fixtures in tests and the public demo.
- Keep credentials and local data out of commits. Stage named paths after checking the staged diff. Use imperative commit subjects and coherent changes with associated tests and documentation.
- Consult the private workspace review guide when available. The README is the public record of scope, acceptance criteria, and phase status.
