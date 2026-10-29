"""Every Lectern setting documented in .env.example reaches the container in both compose
variants: compose.yaml and deploy/tailscale/compose.yaml (+ lan.yaml). Compose only passes
what a service's `environment:` lists, so a key missing there is silently ignored.

Checks key names only; values are never read or printed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lectern.config import Settings

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
VARIANTS = {
    "compose.yaml": [ROOT / "compose.yaml"],
    "deploy/tailscale": [
        ROOT / "deploy" / "tailscale" / "compose.yaml",
        ROOT / "deploy" / "tailscale" / "lan.yaml",
    ],
}
# Only meaningful with the LAN listener, which only deploy/tailscale/lan.yaml turns on.
NOT_IN_ROOT_COMPOSE = {"LECTERN_LAN_URL"}
# Deprecated aliases are gone from .env.example but stay passed through, with an empty default:
# a default would set the old key on every install and make it warn.
DEPRECATED_ALIASES = {"LECTERN_STORAGE_WARN_FREE_PERCENT"}


def _documented_settings() -> set[str]:
    settings = {"LECTERN_" + name.upper() for name in Settings.model_fields}
    keys = {
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text().splitlines()
        if re.match(r"^[A-Z_]+=", line)
    }
    # The rest (TS_*, LECTERN_UID, *_PATH, LECTERN_EXTRAS, ...) are host-side compose
    # variables, which test_deploy_tailscale.py checks against the compose files.
    return keys & settings


def _passed_through(path: Path) -> set[str]:
    """Keys of the lectern service's environment that interpolate the same variable."""
    block = re.search(r"^  lectern:\n(.*?)(?=^  \S|^\S|\Z)", path.read_text(), re.M | re.S)
    assert block, f"no lectern service in {path.name}"
    pairs = re.findall(r"^      (LECTERN_\w+): \$\{(\w+)", block.group(1), re.M)
    return {key for key, var in pairs if key == var}


@pytest.mark.parametrize("variant", VARIANTS)
def test_env_example_settings_are_passed_through(variant):
    expected = _documented_settings()
    assert len(expected) > 40  # the parser found the file's settings
    if variant == "compose.yaml":
        expected -= NOT_IN_ROOT_COMPOSE
    passed = set().union(*(_passed_through(p) for p in VARIANTS[variant]))
    missing = sorted(expected - passed)
    assert not missing, f"{variant} does not pass through: {', '.join(missing)}"


@pytest.mark.parametrize("variant", VARIANTS)
def test_deprecated_aliases_are_passed_through_with_no_default(variant):
    text = "".join(p.read_text() for p in VARIANTS[variant])
    for key in DEPRECATED_ALIASES:
        assert re.search(rf"^      {key}: \$\{{{key}:-\}}$", text, re.M), key
        assert key not in _documented_settings()
