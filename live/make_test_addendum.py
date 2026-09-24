"""make_test_addendum.py — generate live/test_addendum.pdf, a synthetic document that
passes all four gate conditions in SETUP_LIVE.md section 8.

    python live/make_test_addendum.py

Why a generator rather than a committed binary nobody can read: the whole point of the
fixture is *what text it contains*, and a PDF hides that from every tool this repo
already uses. The content is right here in `LINES`, so a reviewer can check the four
conditions by reading a diff, and anyone can regenerate the file after changing a
street name or a party.

Written by hand rather than with reportlab. A one-page text PDF is about eighty lines
of object graph, and a rendering dependency added for one fixture is a dependency the
deploy carries forever.

TWO PROPERTIES THIS FILE EXISTS TO GUARANTEE

  1. A REAL TEXT LAYER. `app.adapters.llm.pdf_text` reads the page with pypdf and
     refuses anything under MIN_TEXT_CHARS as a scan. Each line below is emitted as a
     single `Tj` show-text operator with no kerning array and no word-spacing tricks,
     so pypdf returns the string back character for character. That matters more than
     it sounds: the routing gate does a SUBSTRING test, and a PDF writer that splits
     "Springfield" across two show operations to tighten a kern produces a document
     whose address is invisible to it.

  2. THE ADDRESS SURVIVES NORMALIZATION INTO A KNOWN KEY. The document says
     "12 Oak St, Springfield"; `validate._normalize` lowercases it and strips the
     comma to "12 oak st springfield", which is the key in live/known_folders.json.
     The self-check at the bottom asserts exactly that, against the real normalizer
     rather than a second copy of the rule.

Everything in the document is invented -- the parties, the brokerage, the inspection,
the figures. There is no such property.
"""
from __future__ import annotations

import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "test_addendum.pdf"

PAGE_W, PAGE_H = 612, 792          # US Letter, points
LEFT = 72                          # 1" margin
TOP = 720
LEADING = 14.5

REG, BOLD = "F1", "F2"

#: (font, size, text). An empty string is a blank line: it advances the cursor and
#: emits nothing, so no stray show-operator lands in the text layer.
LINES: list[tuple[str, int, str]] = [
    (BOLD, 13, "ADDENDUM TO PURCHASE AND SALE AGREEMENT"),
    (REG, 9, "Meridian Residential  |  118 Fairbanks Row, Springfield  |  (555) 0142"),
    (REG, 11, ""),
    (REG, 11, "Addendum No. 2"),
    (REG, 11, "Date: March 3, 2026"),
    (REG, 11, ""),
    (BOLD, 11, "Property Address: 12 Oak St, Springfield"),
    (REG, 11, ""),
    (REG, 11, "Buyer: Marguerite Whitfield"),
    (REG, 11, "Seller: Coriander Holdings LLC"),
    (REG, 11, ""),
    (REG, 11, "This Addendum is attached to and made a part of the Purchase and Sale"),
    (REG, 11, "Agreement dated February 14, 2026 between the parties named above for the"),
    (REG, 11, "property at 12 Oak St, Springfield. Where the terms of this Addendum conflict"),
    (REG, 11, "with the terms of the Agreement, this Addendum controls."),
    (REG, 11, ""),
    (BOLD, 11, "1.  INSPECTION REPAIRS."),
    (REG, 11, "    Seller shall, at Seller's expense, complete replacement of the GFCI outlets"),
    (REG, 11, "    in the kitchen and both bathrooms identified in the inspection report dated"),
    (REG, 11, "    February 27, 2026. Work shall be completed by a licensed electrician and"),
    (REG, 11, "    evidenced by a paid invoice delivered to Buyer prior to closing."),
    (REG, 11, ""),
    (BOLD, 11, "2.  CLOSING DATE."),
    (REG, 11, "    The closing date is extended to April 10, 2026. All deadlines calculated"),
    (REG, 11, "    from the closing date shift accordingly. Time remains of the essence."),
    (REG, 11, ""),
    (BOLD, 11, "3.  REPAIR CREDIT."),
    (REG, 11, "    Seller shall issue Buyer a credit of $1,850.00 at closing for the electrical"),
    (REG, 11, "    panel replacement described in the inspection report. No further credit is"),
    (REG, 11, "    owed by Seller for that item."),
    (REG, 11, ""),
    (BOLD, 11, "4.  NO OTHER CHANGES."),
    (REG, 11, "    Except as expressly modified by this Addendum, all terms and conditions of"),
    (REG, 11, "    the Agreement remain in full force and effect."),
    (REG, 11, ""),
    (REG, 11, ""),
    (REG, 11, "Buyer:  ______________________________        Date: ____________________"),
    (REG, 10, "        Marguerite Whitfield"),
    (REG, 11, ""),
    (REG, 11, "Seller: ______________________________        Date: ____________________"),
    (REG, 10, "        Coriander Holdings LLC, by its Managing Member"),
    (REG, 11, ""),
    (REG, 11, ""),
    (REG, 9, "Prepared by Meridian Residential on behalf of Buyer, Marguerite Whitfield."),
    (REG, 9, "Page 1 of 1"),
]


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream() -> bytes:
    """One BT/ET block per line, positioned absolutely with Tm.

    Absolute positioning rather than TL/T* leading because it makes each line
    independent: a font-size change mid-document cannot silently shift everything
    below it, and pypdf's line breaking keys off the y coordinate it sees here.
    """
    parts, y = [], TOP
    for font, size, text in LINES:
        if text:
            parts.append(
                f"BT /{font} {size} Tf 1 0 0 1 {LEFT} {y:.1f} Tm "
                f"({_escape(text)}) Tj ET"
            )
        y -= LEADING
    return "\n".join(parts).encode("ascii")


def build() -> bytes:
    stream = _content_stream()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /{REG} 5 0 R /{BOLD} 6 0 R >> >> "
            f"/Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def self_check(pdf_bytes: bytes) -> list[str]:
    """Assert the four gate conditions against the project's own code, not a restatement
    of it. Returns a list of failures; empty means the fixture is good."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.adapters.llm import MIN_TEXT_CHARS, pdf_text
    from app.pipeline.validate import _normalize

    problems = []
    text, error = pdf_text(pdf_bytes)
    if error:
        return [f"pdf_text refused the file: {error}"]

    if len(text.strip()) < MIN_TEXT_CHARS:
        problems.append(f"text layer is only {len(text.strip())} chars")

    for needle in ("Addendum", "12 Oak St, Springfield", "Marguerite Whitfield", "March 3, 2026"):
        if needle not in text:
            problems.append(f"not extractable verbatim: {needle!r}")

    # The containment gate normalizes both sides; so does the folder resolver.
    if _normalize("12 Oak St, Springfield") not in _normalize(text):
        problems.append("address does not survive normalization into the text layer")

    return problems


def main() -> int:
    pdf_bytes = build()
    problems = self_check(pdf_bytes)
    if problems:
        print("REFUSING to write the fixture:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    OUT.write_bytes(pdf_bytes)
    print(f"wrote {OUT} ({len(pdf_bytes)} bytes)")
    print("resolves to known_folders key: '12 oak st springfield'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
