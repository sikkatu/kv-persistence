"""Part B (4c): behavioural tests for the extras compose + relay + rules.

  no-new-ports   : docker-compose.override.yml must not publish ANY port on
                   the host and the relay must never bind 0.0.0.0 / ::.
                   MUTATION caught: a `ports:` block with 0.0.0.0 added.
  tag-pin        : no `:latest` or floating tags in the new images.
                   MUTATION caught: switching a tagged image to `:latest`.
  relay          : the RENDERED nginx conf serves GET /metrics through to a
                   fake upstream and refuses anything else (404/405), and
                   strips Authorization. Real nginx (if installed locally) --
                   otherwise SKIP with a visible reason.
  rules-promtool : `promtool check rules` on the real rules.d/*.yml, if
                   promtool is available -- otherwise SKIP with a reason.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
OBS = KV / "observability"
OVR = OBS / "docker-compose.override.yml"
TPL = OBS / "extras" / "relay" / "nginx.conf.template"


# ---------------------------------------------------------------- no-new-ports
def test_no_new_published_ports_and_no_wide_bind():
    """R7: nothing in the override publishes a host port on ANY address, and
    the nginx template refuses 0.0.0.0 / :: binds.
    MUTATION caught: adding a `ports: - "0.0.0.0:3000:3000"` to a service."""
    s = OVR.read_text()
    assert not re.search(r"^\s+ports:\s*$", s, re.M), (
        "a `ports:` section appeared in docker-compose.override.yml -- "
        "nothing may be published on the host (R7)")
    tpl = TPL.read_text()
    # split off comment lines: only REAL listen directives may be judged
    code = "\n".join(l for l in tpl.splitlines()
                     if not l.strip().startswith("#"))
    for wide in ("0.0.0.0", "listen ::", "listen [::]"):
        assert wide not in code, f"relay must never bind {wide!r}"
    assert "listen @LISTEN_IP@:@RELAY_PORT@;" in code, (
        "the relay must listen on the rendered gateway address only")


# ---------------------------------------------------------------- tag-pin
def test_images_are_pinned_not_latest():
    """R3: every image ref in the override carries an exact tag (no :latest,
    no bare repo name).
    MUTATION caught: `image: nginx:1.29.4-alpine` -> `image: nginx:latest`."""
    s = OVR.read_text()
    images = re.findall(r"^\s+image:\s*(\S+)", s, re.M)
    assert images, "no image: lines found in the override"
    for ref in images:
        assert ":latest" not in ref, f"floating :latest tag: {ref}"
        assert re.search(r":[\w][\w.\-]*$", ref), f"unpinned (no tag): {ref}"


# ---------------------------------------------------------------- relay
def test_nginx_relay_renders_and_strips_auth(tmp_path):
    """Build the rendered conf the way start-observability.sh does and run it
    against a fake upstream when a local `nginx` binary exists (SKIP else)."""
    nginx = shutil.which("nginx")
    if not nginx:
        pytest.skip("SKIP: nginx not installed on this host -- relay behaviour "
                    "checked on the operator stand only")
    tpl = TPL.read_text()
    conf = (tpl.replace("@LISTEN_IP@", "127.0.0.1")
               .replace("@RELAY_PORT@", "31337")
               .replace("@ENGINE_PORT@", "31338"))
    (tmp_path / "nginx.conf").write_text(conf)

    # fake upstream on 31338 that REJECTS anything with Authorization
    import socket
    import threading

    seen = {}

    def upstream():
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 31338))
        srv.listen(5)
        srv.settimeout(15)
        try:
            c, _ = srv.accept()
            data = c.recv(4096).decode("latin1", "replace")
            seen["req"] = data
            if "Authorization" in data:
                body = "AUTH_LEAKED"
            else:
                body = "vllm:num_requests_running 3\n"
            c.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                + body.encode())
            c.close()
        except Exception:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=upstream, daemon=True)
    t.start()

    proc = subprocess.Popen(
        [nginx, "-c", str(tmp_path / "nginx.conf"), "-g", "daemon off;"],
        cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time
        time.sleep(1)
        def req(path, method="GET"):
            r = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "-X", method, "-H", "Authorization: Bearer SECRETKEY",
                 f"http://127.0.0.1:31337{path}"],
                capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        assert req("/metrics") == "200", "GET /metrics must proxy to the engine"
        assert req("/v1/models") == "404", "non-/metrics path must 404"
        assert req("/metrics", "POST") == "405", "non-GET must 405"
        t.join(timeout=3)
        assert "req" in seen, "upstream never saw a request"
        assert "SECRETKEY" not in seen.get("req", ""), (
            "Authorization header must be STRIPPED by the relay")
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------- rules
def test_rules_validate_with_promtool():
    """`promtool check rules` must accept the real rules.d files (SKIP with a
    reason when promtool is unavailable)."""
    pt = shutil.which("promtool")
    if not pt:
        pytest.skip("SKIP: promtool not installed -- rule syntax covered on "
                    "the operator stand only")
    for y in sorted((OBS / "extras" / "rules.d").glob("*.yml")):
        r = subprocess.run([pt, "check", "rules", str(y)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{y.name}: {r.stdout}{r.stderr}"