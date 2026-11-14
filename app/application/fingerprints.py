"""Content identity shared by the local and durable result caches."""

import hashlib
import json
from importlib.metadata import version

from app.domain.parsing import ParseOptions, Tier

# Change when output policy changes, even if the model/parser versions stay the same.
OUTPUT_REVISION = 2


def content_fingerprint(
    payload: bytes, tier: Tier, encode_img: bool, options: ParseOptions, model_revision: str = ""
) -> str:
    """Namespace both caches by every option and the implementation/model revisions."""
    digest = hashlib.sha256(payload)
    digest.update(
        json.dumps(
            [
                OUTPUT_REVISION,
                tier,
                encode_img,
                options.to_dict(),
                version("mineru"),
                model_revision,
            ],
            sort_keys=True,
        ).encode()
    )
    return digest.hexdigest()
