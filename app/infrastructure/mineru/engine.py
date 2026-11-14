"""Reuse lightweight parsers and shared models while keeping request state isolated."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from app.domain.parsing import LEGACY_OPTIONS, ParseOptions, ParseOutput
from app.infrastructure.metrics import record_sdk_stages, timed
from app.infrastructure.mineru.rendering import render_result


def markdown_without_images(result):
    """Compatibility helper: omit image assets while keeping all recognized content."""
    return render_result(result, False, LEGACY_OPTIONS)


class MinerUEngine:
    def __init__(self, tier: str):
        self.tier = tier
        self._parsers = {}
        self._parser_lock = Lock()

    def warmup(self):
        # Generate a tiny document at startup instead of keeping a private sample
        # PDF in the repository or charging initialization to the first caller.
        from PIL import Image, ImageDraw
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        image = Image.new("RGB", (240, 80), "white")
        ImageDraw.Draw(image).text((10, 20), "MinerU warmup", fill="black", font_size=20)
        output = BytesIO()
        document = canvas.Canvas(output)
        document.setFont("Helvetica", 11)
        for line in range(18):
            document.drawString(
                60,
                740 - line * 18,
                "MinerU service warmup: initialize the parser before accepting online traffic.",
            )
        document.drawImage(ImageReader(image), 60, 300, width=240, height=80)
        formula = document.beginText(220, 405)
        formula.setFont("Times-Italic", 24)
        formula.textOut("E = mc")
        formula.setRise(10)
        formula.setFont("Times-Italic", 12)
        formula.textOut("2")
        document.drawText(formula)
        document.save()
        self.parse(output.getvalue(), True)

    def parse(
        self,
        pdf_bytes: bytes,
        encode_img: bool,
        tier: str | None = None,
        options: ParseOptions | None = None,
    ) -> ParseOutput:
        # Public MinerU 4 SDK; model loading is lazy so HTTP startup is inexpensive.
        tier = tier or self.tier
        options = options or ParseOptions()
        parser_key = (tier, options.image_analysis)
        with self._parser_lock:
            if parser_key not in self._parsers:
                from mineru import MinerUParser

                self._parsers[parser_key] = MinerUParser(
                    tier=tier, image_analysis=options.image_analysis
                )
            parser = self._parsers[parser_key]

        # This directory lives until model execution actually finishes, including
        # when the HTTP caller has already timed out or disconnected.
        with TemporaryDirectory(prefix="mineru-") as directory:
            path = Path(directory) / "document.pdf"
            path.write_bytes(pdf_bytes)
            timings = {}
            with timed(timings, "sdk.total"), record_sdk_stages(timings):
                result = parser.parse(path)
            with timed(timings, "markdown.render"):
                markdown = render_result(result, encode_img, options)
            return ParseOutput(markdown=markdown, pages=len(result.pages), timings=timings)

    def close(self):
        for parser in self._parsers.values():
            parser.close()
