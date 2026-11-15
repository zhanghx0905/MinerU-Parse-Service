import pytest

from app.storage import LocalStorage


def test_local_storage_rejects_paths_and_preserves_data(tmp_path):
    store = LocalStorage(tmp_path)
    key = "inputs/" + "a" * 32 + ".pdf"
    store.put(key, b"pdf bytes")
    assert store.get(key) == b"pdf bytes"
    with pytest.raises(ValueError):
        store.get("../private.pdf")
    store.delete(key)
    store.delete(key)
    assert not list(tmp_path.rglob("*.pdf"))
