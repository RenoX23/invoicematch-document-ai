import pytest

from invoicematch.evidence import source_from_pages

_HEADER = "Item ID    Quantity    Unit price    Line total"
_A = "A-10       10          100.00        1000.00"
_B = "B-20       2           50.00         100.00"


def test_reads_layout_cells_and_preserves_page_and_excerpt() -> None:
    page = "\n".join(
        ("Invoice number: INV-01", "  " + _HEADER, "  " + _A, "Invoice total: 1000.00")
    )
    source = source_from_pages([page])

    assert source.complete_table
    assert len(source.rows) == 1
    row = source.rows[0]
    assert (row.item_id, row.quantity, row.unit_price, row.total) == (
        "A-10",
        "10",
        "100.00",
        "1000.00",
    )
    assert row.evidence.page == 1
    assert row.evidence.line == 3
    assert row.evidence.excerpt == _A


def test_missing_unit_price_preserves_the_correct_line_total_cell() -> None:
    row = "A-10       10                        1000.00"
    source = source_from_pages(["\n".join((_HEADER, row, "Invoice total: 1000.00"))])

    assert source.complete_table
    assert source.rows[0].unit_price == ""
    assert source.rows[0].total == "1000.00"


def test_reads_reordered_columns_and_ignores_notes_after_total() -> None:
    header = "Item ID      Unit price      Quantity      Line total"
    row = "A-10         120.00          10            1200.00"
    source = source_from_pages(
        ["\n".join((header, row, "Invoice total: 1200.00", "Note: reference amount 99.00"))]
    )

    assert source.complete_table
    assert source.rows[0].quantity == "10"
    assert source.rows[0].unit_price == "120.00"


def test_continuation_page_needs_its_own_headings() -> None:
    first_page = "\n".join((_HEADER, _A))
    second_page = "\n".join((_HEADER, _B, "Invoice total: 1100.00"))
    source = source_from_pages([first_page, second_page])

    assert source.complete_table
    assert [row.evidence.page for row in source.rows] == [1, 2]

    source = source_from_pages([first_page, "\n".join((_B, "Invoice total: 1100.00"))])

    assert not source.complete_table


def test_table_content_cannot_supply_invoice_identity() -> None:
    source = source_from_pages(
        ["\n".join((_HEADER, _A, "Invoice number: INV-99", "Invoice total: 1000.00"))]
    )

    assert not source.complete_table
    assert [header.name for header in source.headers] == ["total"]


@pytest.mark.parametrize(
    "page",
    [
        "\n".join((_HEADER, _A)),
        "\n".join((_HEADER, _A, _HEADER, "Invoice total: 1000.00")),
        "\n".join((_HEADER, _A, "Invoice total: 1000.00", _B)),
        "\n".join(("x" * 501, _HEADER, _A, "Invoice total: 1000.00")),
    ],
)
def test_unprovable_table_boundaries_do_not_support_a_match(page: str) -> None:
    assert not source_from_pages([page]).complete_table
