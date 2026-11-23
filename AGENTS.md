# InvoiceMatch repository instructions

## Commands

The repository is in Phase 0. The commands below are proposed for Phase 1 and become verified when run on a clean checkout.

| Task | Proposed command |
| --- | --- |
| Setup | Create a Python 3.11+ virtual environment, activate it, and run `python -m pip install -e ".[dev]"`. |
| Run | `docker compose up --build` |
| Test | `python -m pytest` |
| Lint and format check | `python -m ruff check .` and `python -m ruff format --check .` |
| Type-check | `python -m mypy src` |
| Build | `docker compose build` |

## Repository layout

- [README.md](README.md) records the product, phase gates, and Phase 0 design.
- `src/invoicematch/` is the proposed application package for the HTTP entry point, extraction, reconciliation, and persistence.
- `tests/` is the proposed verification suite. `tests/fixtures/` will hold versioned synthetic inputs and expected outcomes.
- `.github/workflows/` will contain CI when the first runnable checks land.

## Conventions

- Follow the [phase gates](README.md#phases). Ask for approval before changing the schema, introducing a dependency or public interface, or editing more than about three files.
- Ask the independent Scrutiny agent to inspect the exact candidate revision at each gate. Record its findings and rerun affected checks after fixes. Advance after the gate passes.
- Use typed public Python functions, explicit errors, exact decimal money, and parameterized database operations. Exercise behavior through its normal entry point. Use synthetic fixtures in tests and the public demo.
- Keep credentials and local data out of commits. Stage named paths after checking the staged diff. Use imperative commit subjects and coherent changes with associated tests and documentation.
- Consult the private workspace review guide when available. The README is the public record of scope, acceptance criteria, and phase status.
