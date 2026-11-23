"""Generate stable, text-only PDF invoices for the local reviewer workflow."""

from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "src" / "invoicematch" / "samples"

SAMPLES = {
    "clean-match.pdf": (
        "Invoice number: INV-DEMO-01",
        "Supplier ID: SUP-001",
        "Currency: INR",
        "",
        "Item ID    Quantity    Unit price    Line total",
        "A-10       10          100.00        1000.00",
        "B-20       2           50.00         100.00",
        "",
        "Invoice total: 1100.00",
    ),
    "price-mismatch.pdf": (
        "Supplier ID: SUP-001",
        "Invoice number: INV-DEMO-02",
        "Currency: INR",
        "",
        "Item ID      Unit price      Quantity      Line total",
        "A-10         120.00          10            1200.00",
        "B-20         50.00           2             100.00",
        "",
        "Invoice total: 1300.00",
    ),
    "ambiguous-extraction.pdf": (
        "Invoice number: INV-DEMO-03",
        "Supplier ID: SUP-001",
        "Currency: INR",
        "",
        "Item ID    Quantity    Unit price    Line total",
        "A-10       10                        1000.00",
        "B-20       2           50.00         100.00",
        "",
        "Invoice total: 1100.00",
    ),
}


def _pdf_text(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_pdf(lines: tuple[str, ...]) -> bytes:
    content = ["BT /F1 11 Tf 72 755 Td 17 TL"]
    for line in ("SYNTHETIC INVOICE", "", *lines):
        content.append(f"({_pdf_text(line)}) Tj T*")
    stream = "\n".join((*content, "ET", "")).encode("ascii")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream",
    )
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, item in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode("ascii") + item + b"\nendobj\n")
    xref_offset = len(pdf)
    size = len(objects) + 1
    pdf.extend(f"xref\n0 {size}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(pdf)


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for filename, lines in SAMPLES.items():
        (FIXTURE_DIR / filename).write_bytes(_text_pdf(lines))


if __name__ == "__main__":
    main()
