"""Parsing value objects and scheduling failures, independent of HTTP and MinerU."""

import json
from dataclasses import asdict, dataclass, field
from typing import Literal, get_args

Tier = Literal["flash", "basic", "standard", "advanced"]
TIERS = get_args(Tier)


@dataclass(frozen=True)
class ParseOptions:
    """FastGPT multipart defaults; image analysis is independent of image embedding."""

    keep_header_footer: bool = False
    keep_appendix: bool = True
    image_analysis: bool = False
    chart_analysis: bool = False

    def __post_init__(self):
        if any(type(value) is not bool for value in asdict(self).values()):
            raise ValueError("Parse options must be booleans")

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "ParseOptions":
        return cls(**json.loads(value))


# Existing v1 tasks used the SDK's unfiltered image/chart recognition output.
LEGACY_OPTIONS = ParseOptions(image_analysis=True, chart_analysis=True)


@dataclass(frozen=True)
class ParseOutput:
    """A completed document; pages always counts input pages, even after filtering."""

    markdown: str
    pages: int
    timings: dict[str, float] = field(default_factory=dict)


class InvalidDocument(Exception):
    """A rejected source document, with a stable diagnostic independent of HTTP."""

    def __init__(self, message, status_text):
        super().__init__(message)
        self.status_text = status_text


class DocumentLimitExceeded(InvalidDocument):
    pass


class ServiceBusy(Exception):
    pass


class LeaseLost(Exception):
    pass


WORKER_PROTOCOL_VERSION = 3

STAGES = frozenset(
    {
        "api.upload",
        "api.validate",
        "storage.input",
        "storage.result",
        "queue.wait",
        "sdk.total",
        "markdown.render",
        "job.total",
        "worker.download",
        "worker.parse",
        "model.initialize",
        "vlm.http",
        "vlm.queue",
        *(
            "pdf." + name
            for name in (
                "prepare",
                "cleanup",
                "geometry",
                "ocr_detection",
                "ocr_recognition",
                "text_fill",
                "formulas",
                "table_recognition",
                "formula_numbers",
                "render",
                "layout",
                "table_orientation",
                "native_tables",
                "seals",
                "image_assets",
            )
        ),
    }
)
