"""Verify output semantics against real SDK types without requiring a GPU."""

from itertools import product

import pytest

from app.domain.parsing import ParseOptions
from app.infrastructure.mineru.rendering import render_result

IMAGE = "data:image/png;base64,eA=="


def text(content):
    return [{"type": "text", "content": content}]


def heading(content, level=2):
    return {"type": "paragraph_title", "level": level, "content": text(content)}


def visual(kind, content, *, sub_type=None):
    block = {
        "type": kind,
        "content": [
            {"type": f"{kind}_body", "content": content, "image_base64": IMAGE},
            {"type": f"{kind}_caption", "content": text(f"{kind} caption")},
        ],
    }
    if sub_type:
        block["sub_type"] = sub_type
    return block


def sdk_result(pages):
    from mineru import MiddleJson, ParseResult

    for blocks in pages:
        for index, block in enumerate(blocks):
            block["index"] = index
            block["bbox"] = [0, index / len(blocks), 1, (index + 1) / len(blocks)]
            if block["type"] in {"image", "chart", "table"}:
                block["content"][0]["index"] = index
                block["content"][0]["bbox"] = block["bbox"]
    return ParseResult(
        MiddleJson.from_dict(
            {
                "schema": "docvortex.middle",
                "schema_version": "2.0",
                "metadata": {
                    "file_suffix": "pdf",
                    "producer": {"name": "mineru", "version": "4.0.12"},
                },
                "pages": [
                    {"page_idx": index, "blocks": blocks} for index, blocks in enumerate(pages)
                ],
                "is_full_document": True,
            }
        )
    )


@pytest.mark.parametrize("flags", list(product((False, True), repeat=4)))
@pytest.mark.parametrize("encode_img", [False, True])
def test_all_fastgpt_options_are_independent_and_do_not_mutate_result(flags, encode_img):
    options = ParseOptions(*flags)
    result = sdk_result(
        [
            [
                {"type": "header", "content": text("HEADER_SENTINEL")},
                {"type": "footer", "content": text("FOOTER_SENTINEL")},
                {"type": "page_number", "content": text("PAGE_SENTINEL")},
                {
                    "type": "text",
                    "content": text("Ordinary body mentions Appendix A without a heading"),
                },
                visual("image", "IMAGE_ANALYSIS_SENTINEL"),
                visual("image", "SEAL_ANALYSIS_SENTINEL", sub_type="seal"),
                visual("chart", "<table><tr><td>CHART_ANALYSIS_SENTINEL</td></tr></table>"),
                visual("table", "<table><tr><td>TABLE_SENTINEL</td></tr></table>"),
                {"type": "equation", "content": "E=mc^2"},
                heading("Appendix A: Supplementary material"),
                {"type": "text", "content": text("APPENDIX_SENTINEL")},
            ],
            [
                heading("Nested appendix section", 3),
                {"type": "text", "content": text("APPENDIX_NESTED_SENTINEL")},
                heading("References"),
                {"type": "text", "content": text("REFERENCES_SENTINEL")},
            ],
        ]
    )
    original = result.middle_json.model_dump_json()
    markdown = render_result(result, encode_img, options).replace(r"\_", "_")
    for sentinel in ("HEADER_SENTINEL", "FOOTER_SENTINEL", "PAGE_SENTINEL"):
        assert (sentinel in markdown) == options.keep_header_footer
    for sentinel in ("APPENDIX_SENTINEL", "APPENDIX_NESTED_SENTINEL"):
        assert (sentinel in markdown) == options.keep_appendix
    for sentinel in ("IMAGE_ANALYSIS_SENTINEL", "SEAL_ANALYSIS_SENTINEL"):
        assert (sentinel in markdown) == options.image_analysis
    assert ("CHART_ANALYSIS_SENTINEL" in markdown) == options.chart_analysis
    assert (IMAGE in markdown) == encode_img
    for expected in (
        "TABLE_SENTINEL",
        "REFERENCES_SENTINEL",
        "E=mc^2",
        "image caption",
        "chart caption",
    ):
        assert expected in markdown
    assert "Ordinary body mentions Appendix" in markdown
    assert result.middle_json.model_dump_json() == original


@pytest.mark.parametrize("title", ["附录 A 补充说明", "附錄：資料", "Appendices", "12. Appendix B"])
def test_appendix_heading_recognition(title):
    result = sdk_result([[heading(title), {"type": "text", "content": text("REMOVE_ME")}]])
    assert not render_result(result, True, ParseOptions(keep_appendix=False)).strip()


def test_parser_instances_follow_analysis_policy(monkeypatch):
    from app.infrastructure.mineru.engine import MinerUEngine

    created = []

    class Parser:
        def __init__(self, *, tier, image_analysis):
            created.append((tier, image_analysis))

        def parse(self, _path):
            return sdk_result([[visual("image", "Recognized image")]])

        def close(self):
            pass

    monkeypatch.setattr("mineru.MinerUParser", Parser)
    engine = MinerUEngine("advanced")
    try:
        assert "Recognized image" not in engine.parse(b"pdf", True).markdown
        assert (
            "Recognized image"
            in engine.parse(b"pdf", True, options=ParseOptions(image_analysis=True)).markdown
        )
        engine.parse(b"pdf", True, options=ParseOptions(keep_header_footer=True))
        assert created == [("advanced", False), ("advanced", True)]
    finally:
        engine.close()
