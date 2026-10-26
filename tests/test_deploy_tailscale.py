"""Invariants of the tracked Tailscale deployment (deploy/tailscale/, .env.example).

The compose checks run `docker compose config`, so they see the file exactly as Compose
resolves it from the repository root. CI sets COMPOSE_REQUIRED so they cannot silently skip.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy" / "tailscale"
COMPOSE = DEPLOY / "compose.yaml"
LAN = DEPLOY / "lan.yaml"
SERVE = DEPLOY / "serve.json"
ENV_EXAMPLE = ROOT / ".env.example"
TEST_ENV = ROOT / "tests" / "fixtures" / "deploy" / "tailscale.env"


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    return probe.returncode == 0


@pytest.fixture(scope="module")
def compose_config():
    if not _compose_available():
        if os.environ.get("COMPOSE_REQUIRED"):
            pytest.fail("COMPOSE_REQUIRED is set but `docker compose` is not available")
        pytest.skip("docker compose not installed")

    def run(env_file: Path, *overlays: Path) -> subprocess.CompletedProcess[str]:
        # Shell variables override --env-file in Compose; keep the test hermetic.
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("LECTERN_", "TS_", "COMPOSE_"))
        }
        return subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE)]
            + [arg for o in overlays for arg in ("-f", str(o))]
            + ["config", "--format", "json"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    return run


def _load(run, env_file: Path, *overlays: Path) -> dict:
    result = run(env_file, *overlays)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("env_file", [ENV_EXAMPLE, TEST_ENV], ids=["env-example", "overrides"])
def test_nothing_is_published_and_lectern_shares_the_sidecar_network(compose_config, env_file):
    cfg = _load(compose_config, env_file)
    services = cfg["services"]
    assert set(services) == {"tailscale", "lectern"}
    for name, svc in services.items():
        assert not svc.get("ports"), f"{name} publishes ports"
        # No Docker socket for either container.
        assert not any("docker.sock" in str(v.get("source", "")) for v in svc.get("volumes", [])), (
            name
        )
        assert svc.get("network_mode") != "host", name
    lectern, ts = services["lectern"], services["tailscale"]
    assert lectern["network_mode"] == "service:tailscale"
    assert lectern["depends_on"]["tailscale"]["condition"] == "service_healthy"
    # Only tailscale serve (loopback) may set X-Forwarded-For, whatever .env says.
    assert lectern["environment"]["LECTERN_TRUSTED_PROXIES"] == "127.0.0.1"
    assert "LECTERN_INSECURE_HTTP" not in lectern["environment"]
    assert "LECTERN_TRUST_CF_CONNECTING_IP" not in lectern["environment"]
    assert lectern["read_only"] is True
    assert lectern["cap_drop"] == ["ALL"]
    assert ts["environment"]["TS_SERVE_CONFIG"] == "/config/serve.json"
    assert ts["environment"]["TS_USERSPACE"] == "false"
    # The health endpoint must not listen on the tailnet address.
    assert ts["environment"]["TS_LOCAL_ADDR_PORT"].startswith("127.0.0.1:")
    assert "TS_EXTRA_ARGS" not in ts["environment"]
    assert re.fullmatch(r"tailscale/tailscale:v[\d.]+@sha256:[0-9a-f]{64}", ts["image"])


def test_paths_resolve_against_the_repository_root(compose_config):
    cfg = _load(compose_config, ENV_EXAMPLE)
    lectern, ts = cfg["services"]["lectern"], cfg["services"]["tailscale"]
    assert Path(lectern["build"]["context"]) == ROOT
    assert lectern["build"]["args"]["LECTERN_EXTRAS"] == ""  # default, non-align image
    # Empty in .env.example must fall back to the app's defaults, not reach it as "".
    assert lectern["environment"]["LECTERN_ALIGN_MODEL"] == "small"
    assert lectern["environment"]["LECTERN_ALIGN_THREADS"] == "0"
    assert lectern["user"] == "1000:1000"
    mounts = {m["target"]: m for m in lectern["volumes"] + ts["volumes"]}
    assert Path(mounts["/data"]["source"]) == ROOT / "data" / "lectern"
    assert Path(mounts["/library"]["source"]) == ROOT / "library"
    assert Path(mounts["/backups"]["source"]) == ROOT / "backups"
    # Created if missing so `git pull && up` works without it; backups are off by default.
    assert mounts["/backups"]["bind"]["create_host_path"] is True
    assert Path(mounts["/var/lib/tailscale"]["source"]) == ROOT / "data" / "tailscale"
    assert Path(mounts["/config/serve.json"]["source"]) == SERVE
    assert mounts["/config/serve.json"]["read_only"] is True
    # Missing data/library must fail instead of appearing as root-owned, unwritable dirs.
    # (Compose omits create_host_path when false; short syntax sets it true.)
    for target in ("/data", "/library"):
        assert not mounts[target].get("bind", {}).get("create_host_path", False), target
    assert lectern["environment"]["LECTERN_BASE_URL"] == "https://lectern.tail1234.ts.net"
    assert ts["environment"]["TS_HOSTNAME"] == "lectern"


def test_env_overrides_apply(compose_config):
    cfg = _load(compose_config, TEST_ENV)
    lectern, ts = cfg["services"]["lectern"], cfg["services"]["tailscale"]
    mounts = {m["target"]: m["source"] for m in lectern["volumes"] + ts["volumes"]}
    assert mounts["/data"] == "/srv/lectern/data"
    assert mounts["/library"] == "/srv/lectern/library"
    assert mounts["/var/lib/tailscale"] == "/srv/lectern/tailscale"
    assert mounts["/backups"] == "/srv/lectern/backups"
    assert lectern["user"] == "1001:1002"
    assert lectern["environment"]["LECTERN_BASE_URL"] == "https://books.tail1234.ts.net"
    assert lectern["environment"]["LECTERN_LOG_LEVEL"] == "DEBUG"
    assert lectern["build"]["args"]["LECTERN_EXTRAS"] == "align tts"
    assert lectern["environment"]["LECTERN_ALIGN_MODEL"] == "base"
    assert lectern["environment"]["LECTERN_ALIGN_THREADS"] == "4"
    assert ts["environment"]["TS_HOSTNAME"] == "books"
    assert ts["environment"]["TS_AUTHKEY"] == "tskey-auth-not-a-real-key"


def test_missing_base_url_is_an_error(compose_config, tmp_path):
    env_file = tmp_path / "empty.env"
    env_file.write_text("TS_AUTHKEY=tskey-auth-x\n")
    result = compose_config(env_file)
    assert result.returncode != 0
    assert "LECTERN_BASE_URL" in result.stderr


def test_lan_overlay_publishes_one_private_address_to_the_lan_listener(compose_config, tmp_path):
    """deploy/tailscale/lan.yaml (docs/home-network.md): opt-in, one host address, and nothing
    else about the deployment changes."""
    env = tmp_path / "lan.env"
    base = ENV_EXAMPLE.read_text()
    env.write_text(base.replace("LECTERN_LAN_BIND=\n", "LECTERN_LAN_BIND=192.168.1.20:8080\n"))
    plain = _load(compose_config, ENV_EXAMPLE)
    cfg = _load(compose_config, env, LAN)
    ts, lectern = cfg["services"]["tailscale"], cfg["services"]["lectern"]
    assert ts["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "192.168.1.20",
            "target": 8081,
            "published": "8080",
            "protocol": "tcp",
        }
    ]
    assert not lectern.get("ports")
    assert lectern["environment"]["LECTERN_LAN_PORT"] == "8081"
    assert lectern["environment"]["LECTERN_LAN_URL"] == "http://192.168.1.20:8080"
    # Everything else is exactly the plain deployment.
    for name in ("tailscale", "lectern"):
        a, b = dict(plain["services"][name]), dict(cfg["services"][name])
        a.pop("ports", None), b.pop("ports", None)
        b["environment"] = {
            k: v
            for k, v in b["environment"].items()
            if k not in ("LECTERN_LAN_PORT", "LECTERN_LAN_URL")
        }
        assert a == b, name

    env.write_text(
        env.read_text().replace("LECTERN_LAN_URL=\n", "LECTERN_LAN_URL=http://lectern.local:8080\n")
    )
    cfg = _load(compose_config, env, LAN)
    assert (
        cfg["services"]["lectern"]["environment"]["LECTERN_LAN_URL"] == "http://lectern.local:8080"
    )

    # Without an address the overlay refuses to start rather than publish on every interface.
    result = compose_config(ENV_EXAMPLE, LAN)
    assert result.returncode != 0 and "LECTERN_LAN_BIND" in result.stderr


def test_env_example_documents_every_compose_variable():
    used = set(re.findall(r"\$\{(\w+)", COMPOSE.read_text() + LAN.read_text()))
    documented = {
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text().splitlines()
        if re.match(r"^[A-Z_]+=", line)
    }
    assert used == documented
    assert "COMPOSE_FILE" not in documented


def test_serve_config_is_tailnet_https_to_loopback_only():
    serve = json.loads(SERVE.read_text())
    assert set(serve) == {"TCP", "Web"}  # no AllowFunnel, no other listeners
    assert serve["TCP"] == {"443": {"HTTPS": True}}
    assert serve["Web"] == {
        "${TS_CERT_DOMAIN}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}
    }


def test_local_deployment_files_are_git_ignored():
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    paths = [".env", ".env.local", "data/lectern/lectern.db", "library/a.epub", "backups/x"]
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert sorted(ignored) == sorted(paths)
    tracked = subprocess.run(
        ["git", "check-ignore", "--no-index", ".env.example"], cwd=ROOT, check=False
    )
    assert tracked.returncode == 1  # 1 = not ignored
