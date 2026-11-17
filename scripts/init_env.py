"""Create a commented .env, preserve overrides, and sync the image tag from pyproject.toml."""

import argparse
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def initialize(path):
    # Read only the static project version; this tool also runs on hosts without
    # tomllib or the application's Python dependencies installed.
    project = (ROOT / "pyproject.toml").read_text().split("[project]", 1)[1].split("\n[", 1)[0]
    version = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)[1]
    values = {}
    comments = {}
    preceding = []
    if path.exists():
        for line in path.read_text().splitlines():
            match = ASSIGNMENT.fullmatch(line)
            if match:
                key, value = match.groups()
                values[key] = value
                comments[key] = preceding
                preceding = []
            elif line.startswith("#"):
                preceding.append(line)
            else:
                preceding = []

    # The old four-GPU knob took precedence over PARSE_WORKERS. Fold that
    # effective value into the single knob when the selected topology is known.
    legacy = values.pop("WORKERS_PER_GPU", None)
    if legacy is not None and "compose.quad.yaml" in values.get("COMPOSE_FILE", ""):
        values["PARSE_WORKERS"] = legacy
    values["SERVICE_VERSION"] = version.replace("+", "-")
    token = values.get("WORKER_API_TOKEN", "").strip().strip("\"'")
    if not token or token.startswith("replace-"):
        values["WORKER_API_TOKEN"] = secrets.token_hex(32)

    output = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        match = ASSIGNMENT.fullmatch(line)
        if match:
            key, default = match.groups()
            line = key + "=" + values.pop(key, default)
        elif line.startswith("# ") and (match := ASSIGNMENT.fullmatch(line[2:])):
            key, _ = match.groups()
            if key in values:
                line = key + "=" + values.pop(key)
        output.append(line)
    if values:
        output += ["", "# 保留的显式覆盖；删除该项即可恢复默认值，含义见 docs/scaling.md。", ""]
        for key, value in values.items():
            output.extend(comments.get(key) or [f"# {key}：本机显式覆盖。"])
            output.append(key + "=" + value)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("\n".join(output) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    initialize(args.file)
    print(".env initialized; overrides preserved, image tag synced, credentials kept private")


if __name__ == "__main__":
    main()
