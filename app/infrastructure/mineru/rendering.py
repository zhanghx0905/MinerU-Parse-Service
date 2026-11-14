"""Apply FastGPT output policy to a copy of MinerU's structured document."""

import re

from app.domain.parsing import ParseOptions

_APPENDIX_TITLE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*|[IVX]+)[.、:：\s-]+)?(?:附\s*[录錄]|appendi(?:x|ces)\b)",
    re.IGNORECASE,
)


def _title_text(block):
    """Read heading text without matching body paragraphs or table-of-contents entries."""
    from mineru.types import TitleBlockBase

    if not isinstance(block, TitleBlockBase):
        return None

    def text(span):
        content = getattr(span, "content", "")
        return content if isinstance(content, str) else "".join(text(child) for child in content)

    return "".join(text(span) for span in block.content).strip()


def _filter_sections(document, options):
    """Remove explicit appendix sections across pages until the next peer/ancestor heading."""
    from mineru.types import TextBlock

    appendix_level = None
    for page in document.pages:
        retained = []
        for block in page.blocks:
            title = _title_text(block)
            if not options.keep_appendix and title is not None:
                if _APPENDIX_TITLE.match(title):
                    appendix_level = (
                        min(appendix_level, block.level) if appendix_level else block.level
                    )
                elif appendix_level is not None and block.level <= appendix_level:
                    appendix_level = None
            if appendix_level is not None:
                continue
            if block.type in {"header", "footer", "page_number"}:
                if not options.keep_header_footer:
                    continue
                # Default SDK rendering drops auxiliary blocks. Promote only requested
                # headers/footers, retaining normal cross-page paragraph merging.
                block = TextBlock(
                    type="text", content=block.content, index=block.index, bbox=block.bbox
                )
            retained.append(block)
        page.blocks = retained


def render_result(result, encode_img: bool, options: ParseOptions) -> str:
    """Preserve captions, tables and equations while independently filtering analysis/assets."""
    from bs4 import BeautifulSoup
    from mineru.render import render_markdown
    from mineru.types import BlockBase, ImagePayloadBlock, ImagePayloadContentBlock

    document = result.middle_json.model_copy(deep=True)
    _filter_sections(document, options)
    pending = [block for page in document.pages for block in page.blocks]
    while pending:
        block = pending.pop()
        if (block.type == "image_body" and not options.image_analysis) or (
            block.type == "chart_body" and not options.chart_analysis
        ):
            # Image and seal recognition live in image_body.content; captions are
            # separate children. Chart pixels remain available when analysis is off.
            block.content = ""
        if not encode_img:
            if isinstance(block, ImagePayloadBlock):
                block.image_base64 = block.image_path = block.image_url = None
            if isinstance(block, ImagePayloadContentBlock) and "<img" in block.content.lower():
                fragment = BeautifulSoup(block.content, "html.parser")
                for image in fragment.find_all("img"):
                    image.decompose()
                block.content = str(fragment)
        content = getattr(block, "content", None)
        if isinstance(content, list):
            pending.extend(child for child in content if isinstance(child, BlockBase))

    return render_markdown(document, image_renderer=None if encode_img else lambda _block: "")
