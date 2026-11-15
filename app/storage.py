"""Immutable object storage; object keys never contain supplied file names."""

import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

_KEY = re.compile(r"(?:inputs|results)/[a-f0-9]{32}\.(?:pdf|md)\Z")


def validate_key(key):
    if not _KEY.fullmatch(key):
        raise ValueError("Invalid object key")


class LocalStorage:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key, data):
        validate_key(key)
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(dir=target.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            descriptor = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def get(self, key):
        validate_key(key)
        return (self.root / key).read_bytes()

    def delete(self, key):
        validate_key(key)
        (self.root / key).unlink(missing_ok=True)
