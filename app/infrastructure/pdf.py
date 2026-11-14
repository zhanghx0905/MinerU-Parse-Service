"""Input validation kept separate from HTTP orchestration and model execution."""

import io

from pypdf import PdfReader

from app.domain.parsing import DocumentLimitExceeded, InvalidDocument


def validate_pdf(payload: bytes, max_pages: int) -> int:
    """Validate before scheduling, returning original page count for budgets and billing."""
    if b"%PDF-" not in payload[:1024]:
        raise InvalidDocument("Invalid PDF signature", "pdfCorrupted")
    try:
        reader = PdfReader(io.BytesIO(payload))
        if reader.is_encrypted and not reader.decrypt(""):
            raise InvalidDocument("Password-protected PDFs are not supported", "pdfEncrypted")
        pages = len(reader.pages)
    except InvalidDocument:
        raise
    except Exception as exc:
        raise InvalidDocument("Invalid or damaged PDF", "pdfCorrupted") from exc
    if not pages:
        raise InvalidDocument("PDF has no pages", "invalidParseFile")
    if pages > max_pages:
        raise DocumentLimitExceeded(f"PDF exceeds {max_pages} pages", "invalidParseFile")
    return pages
