"""Small PDF inputs and queue submissions shared across regression tests."""

import io
from uuid import uuid4

from pypdf import PdfWriter


def make_pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def submit(store, fingerprint=None, pages=1, **kwargs):
    return store.submit(
        fingerprint=fingerprint or uuid4().hex,
        input_key=f"inputs/{uuid4().hex}.pdf",
        pages=pages,
        tier="standard",
        encode_img=True,
        **kwargs,
    )
