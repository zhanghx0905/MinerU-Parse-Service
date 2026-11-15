"""One document per process; model reuse belongs to the MinerU SDK."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models import ParseOptions, ParseOutput, Tier
from app.parser.rendering import render_result
from app.parser.timing import record_sdk_stages, timed


def warmup(tier: Tier):
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
    parse_pdf(output.getvalue(), True, tier, ParseOptions())


def parse_pdf(pdf_bytes: bytes, encode_img: bool, tier: Tier, options: ParseOptions) -> ParseOutput:
    from mineru import MinerUParser

    with TemporaryDirectory(prefix="mineru-") as directory:
        path = Path(directory) / "document.pdf"
        path.write_bytes(pdf_bytes)
        timings = {}
        with MinerUParser(tier=tier, image_analysis=options.image_analysis) as parser:
            with timed(timings, "sdk.total"), record_sdk_stages(timings):
                result = parser.parse(path)
        pages = len(result.pages)
        with timed(timings, "markdown.render"):
            markdown = render_result(result, encode_img, options)
        return ParseOutput(markdown=markdown, pages=pages, timings=timings)
