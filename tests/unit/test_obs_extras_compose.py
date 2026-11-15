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


# ------------------------------------------------- dcgm mount + command + caps
def _block(service: str) -> str:
    """Extract the YAML block of one top-level service (indent 2) from the
    override file WITHOUT requiring pyyaml -- the file is small and regular."""
    lines = OVR.read_text().splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == f"{service}:" and ln.startswith("  ") and not ln.startswith("   "):
            start = i
            break
    assert start is not None, f"service {service} not found in the override"
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.strip() == "":        # keep blank lines inside the block
            out.append(ln)
            continue
        if ln.startswith("  ") and not ln.startswith("   "):
            break                    # next top-level service
        out.append(ln)
    return "\n".join(out)


def test_dcgm_counter_file_mount():
    """The DCGM exporter must READ the overrides' counters CSV: the file is
    mounted at /etc/dcgm-exporter/default-counters.csv AND the container is
    started with `command: ["-f", "/etc/dcgm-exporter/default-counters.csv"]`.
    MUTATION caught: mounting at /etc/dcgm-exporter/dcp-counters.csv (which
    the exporter never reads) or dropping the `command:` line."""
    b = _block("dcgm-exporter")
    assert "./extras/gpu/dcgm-counters.csv:/etc/dcgm-exporter/default-counters.csv:ro" in b, (
        "base counters CSV must be mounted at /etc/dcgm-exporter/default-counters.csv")
    assert 'command: ["-f", "/etc/dcgm-exporter/default-counters.csv"]' in b, (
        "dcgm-exporter must point at the mounted file with command -f")
    assert "dcp-counters" not in b, (
        "dcp-counters.csv is the stock file the exporter ignores -- never mount there")


def test_dcgm_prof_needs_sys_admin_and_base_does_not():
    """KVP_OBS_GPU_PROF is implemented as a separate dcgm-exporter-prof
    service (profile gpu-prof): it carries cap_add: [SYS_ADMIN]; the BASE
    dcgm-exporter service must NOT (least privilege).
    MUTATION caught: adding cap_add to the base service, or shipping the prof
    service without SYS_ADMIN."""
    base = _block("dcgm-exporter")
    prof = _block("dcgm-exporter-prof")
    assert "cap_add" not in base, "the base dcgm-exporter must run WITHOUT extra caps"
    assert re.search(r"cap_add:\s*\[SYS_ADMIN\]", prof), (
        "dcgm-exporter-prof needs cap_add: [SYS_ADMIN] (CUPTI profiling fields)")
    assert 'profiles: ["gpu-prof"]' in prof, (
        "dcgm-exporter-prof is activated under the gpu-prof profile")
    assert "./extras/gpu/dcgm-counters.live.csv:/etc/dcgm-exporter/default-counters.csv:ro" in prof, (
        "prof counters CSV must be mounted at the default-counters path")


# ------------------------------------------------- inactive exporter cleanup
def test_extras_up_stops_inactive_gpu_services_with_profile_flag():
    """(r89c #3) extras-up.sh must stop the GPU exporters NOT selected this
    run BEFORE `compose up -d`, using the documented `--profile '*'` FLAG
    (same as stop-all.sh) -- the COMPOSE_PROFILES="*" env form is NOT
    documented for the wildcard and a failure there was swallowed into a WARN.
    MUTATION caught: switching to COMPOSE_PROFILES="*" ... or dropping the
    whole cleanup (the old exporter keeps running next to the renamed one)."""
    s = (KV / "scripts" / "extras-up.sh").read_text()
    assert "docker compose --profile '*' rm -sf" in s, (
        "inactive GPU exporters must be removed with `docker compose --profile '*' rm -sf`")
    assert 'COMPOSE_PROFILES="*" docker compose rm' not in s, (
        "the undocumented COMPOSE_PROFILES='*' env form must not be used for the wildcard")
    # the cleanup must come BEFORE the real up call (old exporter dies first) --
    # match the ACTION line (`COMPOSE_PROFILES="$PROFILES" docker compose up -d`),
    # not the prose mention in the comment above.
    assert s.index("--profile '*' rm -sf") < s.index('COMPOSE_PROFILES="$PROFILES" docker compose up -d'), (
        "stop the inactive exporters BEFORE the compose up -d call")

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