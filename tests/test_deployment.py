"""Retained deployment contracts: effective settings, GPU routing and release identity."""

import json
import os
import subprocess
from importlib.metadata import requires, version
from pathlib import Path

import pytest

from scripts.init_env import initialize

ROOT = Path(__file__).resolve().parents[1]


def test_initialize_preserves_credentials_overrides_and_migrates_four_gpu_count(tmp_path):
    config = tmp_path / ".env"
    config.write_text(
        "COMPOSE_FILE=compose.yaml:compose.quad.yaml\n"
        "PARSE_WORKERS=4\nWORKERS_PER_GPU=6\n"
        "WORKER_API_TOKEN='existing-token'\nPDF_API_KEY=existing-api-key\n"
        "# Larger lease for this host.\nQUEUE_LEASE_SECONDS=120\n"
    )
    initialize(config)
    first = config.read_text()
    assert "PARSE_WORKERS=6\n" in first
    assert "WORKERS_PER_GPU" not in first
    assert "WORKER_API_TOKEN='existing-token'" in first
    assert "PDF_API_KEY=existing-api-key" in first
    assert "# Larger lease for this host.\nQUEUE_LEASE_SECONDS=120" in first
    assert config.stat().st_mode & 0o777 == 0o600
    initialize(config)
    assert config.read_text() == first


def test_initialize_single_gpu_keeps_its_effective_count_and_generates_token(tmp_path):
    config = tmp_path / ".env"
    config.write_text("PARSE_WORKERS=3\nWORKERS_PER_GPU=8\nWORKER_API_TOKEN=\n")
    initialize(config)
    assert "PARSE_WORKERS=3\n" in config.read_text()
    token = config.read_text().split("\nWORKER_API_TOKEN=", 1)[1].splitlines()[0]
    assert len(token) == 64
    initialize(config)
    assert "\nWORKER_API_TOKEN=" + token + "\n" in config.read_text()


@pytest.mark.parametrize("gpus,overlay", [(1, None), (2, "scale"), (4, "quad")])
def test_compose_preserves_gpu_routing_and_injects_only_explicit_app_overrides(
    tmp_path, gpus, overlay
):
    config = tmp_path / ".env"
    config.write_text(
        "PARSE_WORKERS=6\nMAX_PENDING=123\nQUEUE_LEASE_SECONDS=120\n"
        "QUEUE_HEARTBEAT_SECONDS=20\nGPU_ID=1\nGPU_ID_B=2\nGPU_ID_C=4\nGPU_ID_D=5\n"
        "MODEL_REVISION=test-model\n"
    )
    initialize(config)
    command = ["docker", "compose", "--env-file", str(config), "-f", "compose.yaml"]
    if overlay:
        command += ["-f", f"compose.{overlay}.yaml"]
    command += ["config", "--format", "json"]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "ENV_FILE": str(config)},
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("Docker Compose is unavailable")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert len(services) == 2 + 2 * gpus
    api = services["mineru-api"]
    assert api["command"][api["command"].index("--workers") + 1] == "1"
    assert api["volumes"][0]["target"] == "/data"
    assert api["environment"]["MAX_PENDING"] == "123"
    assert api["environment"]["LD_LIBRARY_PATH"] == "/opt/sqlite/lib"
    # No duplicate representation of the code's default in Compose/.env.
    assert "QUEUE_MAX_UPLOADS" not in api["environment"]
    vip = 0
    suffixes = ["", "-b", "-c", "-d"][:gpus]
    for suffix, gpu in zip(suffixes, ["1", "2", "4", "5"], strict=False):
        worker = services["mineru-worker" + suffix]
        vlm = services["mineru-vlm" + suffix]
        assert worker["environment"]["PARSE_WORKERS"] == "6"
        assert worker["environment"]["QUEUE_LEASE_SECONDS"] == "120"
        assert worker["environment"]["QUEUE_HEARTBEAT_SECONDS"] == "20"
        assert worker["environment"]["WORKER_API_TOKEN"] == api["environment"]["WORKER_API_TOKEN"]
        assert worker["depends_on"]["mineru-vlm-gateway"]["condition"] == "service_healthy"
        vip += int(worker["environment"].get("PRIORITY_WORKERS", 0))
        for service in (worker, vlm):
            assert service["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"] == [
                gpu
            ]
        assert vlm["command"][0] == "/models/models/test-model"
    assert vip == (2 if gpus == 4 else 0)
    gateway = services["mineru-vlm-gateway"]
    assert "WORKER_API_TOKEN" not in gateway["environment"]
    assert "PDF_API_KEY" not in gateway["environment"]
    assert gateway["environment"]["VLM_UPSTREAMS"].split(",") == [
        f"http://mineru-vlm{suffix}:8000" for suffix in suffixes
    ]
    assert all("mineru-vlm" + suffix in gateway["depends_on"] for suffix in suffixes)
    # The project is the authority for both installed service identity and image tags.
    service_version = version("mineru-pdf-service")
    sdk = next(
        dep.split("==")[1] for dep in requires("mineru-pdf-service") if dep.startswith("mineru==")
    )
    assert service_version.endswith("+mineru." + sdk)
    assert f'version = "{service_version}"' in (ROOT / "pyproject.toml").read_text()
    tag = service_version.replace("+", "-")
    assert api["image"] == "mineru-api:" + tag
    assert gateway["image"] == api["image"]
    assert all(
        services["mineru-worker" + suffix]["image"] == "mineru-worker:" + tag for suffix in suffixes
    )
