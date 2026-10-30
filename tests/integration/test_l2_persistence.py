"""Integration tests — LMCache MP server + L2 POSIX persistence (CPU-friendly).

Requires `lmcache` (pinned 0.5.3) on PATH — see docs/deployment.md §3.
Skipped automatically when the CLI is absent so `pytest` stays green on
machines without it.

Three separated evidence levels (review P1-2) — never conflate them:

  1. FILE-LEVEL SMOKE  (`test_l2_file_survives_server_restart`)
     Manually written bytes survive process termination. Does NOT prove
     LMCache can re-serve the data through its API.

  2. LMCACHE PERSISTENCE ROUND-TRIP  (`test_lmcache_store_restart_retrieve_roundtrip`)
     A real store through the pinned LMCache 0.5.3 MP protocol
     (`lmcache bench server`, engine-driven CPU data path), a full server
     process restart, and a retrieval of the PRE-RESTART objects verified
     byte-for-byte via the tool's own chunk checksums.
     Proves: LMCache itself can recover persisted cache data from L2.
     Does NOT prove: a specific vLLM + model deployment reuses it.

  3. FULL vLLM ACCEPTANCE  (scripts/test-recovery.sh, tests/recovery/)
     Real vLLM workload + model. Separate, hardware-gated.

Pinned API verified from the 0.5.3 release source (lmcache/cli/commands/
bench/server_bench/): `lmcache bench server` speaks the real MP protocol
(LOOKUP / QUERY_PREFETCH_STATUS / PREPARE_STORE / COMMIT_STORE /
PREPARE_RETRIEVE / COMMIT_RETRIEVE) and hashes client-side KV bytes with
`_compute_client_checksums`, printing `CHECKSUM: <digest> (N chunks)`.

KVP_LIVE_LMCACHE=1 is required to run the real round-trip (skip policy:
documented external prerequisite — the pinned lmcache package + a POSIX
SHM-capable Linux host)."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))

LMBIN = shutil.which("lmcache")


pytestmark = pytest.mark.skipif(
    LMBIN is None, reason="lmcache CLI not installed (see docs/deployment.md §3)"
)


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p


def _render_adapter(root: Path, budget_gb: str = "1") -> Path:
    tmpl = Path(__file__).resolve().parents[2] / (
        "configs/mimo-v2.6-pro/l2-adapter.json.in")
    out = root / "l2-adapter.rendered.json"
    text = tmpl.read_text()
    text = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))
    text = (text
            .replace("__KVP_CACHE_ROOT__", str(root / "l2"))
            .replace("__KVP_CACHE_BUDGET_GB__", budget_gb)
            .replace("__KVP_EVICT_TRIGGER_WATERMARK__", "0.9")
            .replace("__KVP_EVICT_RATIO__", "0.1"))
    json.loads(text)                      # must be valid JSON after render
    out.write_text(text)
    (root / "l2").mkdir(parents=True, exist_ok=True)
    return out


def _adapter_json_string(adapter: Path) -> str:
    """The pinned 0.5.3 `--l2-adapter` expects the adapter spec as a JSON
    STRING on the command line (lmcache/v1/distributed/l2_adapters/config.py
    `add_l2_adapters_args`: `type=str, metavar="JSON"` -> `json.loads(raw)`),
    not a path to a file. Verified against the 0.5.3 release source and a live
    run: passing a file path fails with
    `ValueError: Invalid JSON for --l2-adapter #1: Expecting value: line 1...`."""
    return json.dumps(json.loads(adapter.read_text()), separators=(",", ":"))


@pytest.fixture()
def cache_env(tmp_path):
    adapter = _render_adapter(tmp_path)
    port = _free_port()
    proc = subprocess.Popen(
        [LMBIN, "server", "--host", "127.0.0.1", "--port", str(port),
         "--l1-size-gb", "1", "--chunk-size", "16",
         "--eviction-policy", "LRU",
         "--eviction-trigger-watermark", "0.9", "--eviction-ratio", "0.1",
         "--l2-adapter", _adapter_json_string(adapter)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(5)
    yield {"port": port, "root": tmp_path / "l2", "proc": proc}
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_l2_adapter_renders_valid_and_bounded(tmp_path):
    adapter = _render_adapter(tmp_path, budget_gb="2")
    spec = json.loads(adapter.read_text())
    assert spec["type"] == "nixl_store_dynamic"
    assert spec["persist_enabled"] is True
    assert spec["backend_params"]["max_capacity_gb"] == "2"
    assert spec["eviction"]["eviction_policy"] == "LRU"


def test_server_starts_and_reports(cache_env):
    assert cache_env["proc"].poll() is None, "lmcache server died on startup"
    assert cache_env["root"].is_dir()


def test_l2_file_survives_server_restart(cache_env):
    """File-level smoke ONLY (NOT a real LMCache recovery test).

    Review P1-2: writing bytes to a file and checking they survive a process
    restart does NOT prove LMCache can rehydrate its index and serve the same
    object through its API after restart. This test is deliberately labelled a
    file-level smoke; the real end-to-end recovery test is
    `test_lmcache_store_restart_retrieve_roundtrip` below, and the full
    hardware acceptance path is `scripts/test-recovery.sh`."""
    l2 = cache_env["root"]
    key = "test-object-key-0001.data"
    (l2 / key).write_bytes(b"x" * 4096)
    cache_env["proc"].terminate()
    cache_env["proc"].wait(timeout=15)
    assert (l2 / key).exists(), "persist_enabled=true must keep data files on shutdown"
    assert (l2 / key).stat().st_size == 4096


def test_refuse_invalid_adapter_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"type": "nixl_store_dynamic", "backend": [oops}')
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad.read_text())


# ---------------------------------------------------------------------------
# Level 2: real LMCache store -> full restart -> retrieve round-trip
# ---------------------------------------------------------------------------
# Interface chosen after verifying the pinned LMCache 0.5.3 release source
# (lmcache/cli/commands/bench/server_bench/{command,helpers}.py), not "main":
#   * `lmcache bench server` drives the REAL MP server protocol:
#     REGISTER_KV_CACHE_ENGINE_DRIVEN_CONTEXT, LOOKUP, QUERY_PREFETCH_STATUS,
#     PREPARE_STORE, COMMIT_STORE, PREPARE_RETRIEVE, COMMIT_RETRIEVE.
#   * Cache identity is token-hash based and deterministic across invocations:
#     IPCCacheServerKey.request_id is `field(compare=False)`, so the same
#     --start/--end/--num-tokens addresses the same objects in a later run --
#     what is stored by one invocation IS retrievable from another one.
#   * The engine-driven CPU data path (`--mode cpu --transfer-mode
#     engine_driven`) needs no GPU, no CUDA and no vLLM: the client allocates
#     POSIX-SHM KV tensors (seed 42 -> reproducible content) and the server
#     exchanges chunk bytes through its SHM pool.
#   * Per-chunk MD5 of the KV bytes is printed as `CHECKSUM: <digest>
#     (N chunks)` (helpers._compute_client_checksums, cold ground truth vs
#     post-retrieve warm hash), and the run summary reports
#     `checksum_ok` / `checksum_fail` counts.
#   * A pass with a FULL hit issues NO STORE (miss_chunks == 0 -> no
#     STORE line), so the post-restart run cannot re-create the payload
#     before reading it; a full hit is additionally proven by the
#     `LOOKUP: N/N chunks hit` line and the RETRIEVE status line.
#
# Evidence the test asserts (all parsed from the pinned tool's own output):
#   Phase C (store):   cold pass LOOKUP 0/N (or <N) + `STORE: stored` +
#                      ground-truth CHECKSUM + at least one data file in L2.
#   Phase D (stop):    first server process fully terminated.
#   Phase E (fresh):   NEW process on the same L2 dir, ready via HTTP
#                      health endpoint.
#   Phase F (retrieve):cold pass of the fresh server finds ALL chunks
#                      (LOOKUP N/N) WITHOUT any `STORE: stored` line, RETRIEVE
#                      succeeds, and the CHECKSUM equals the Phase C digest
#                      -> the bytes came back from the persistent L2 files.
#
# Negative control (same helper, empty L2 dir, same seq): the same workload is
# NOT found (LOOKUP 0/N) -- so a full hit after restart cannot be a false
# positive of the bench tool.
#
# The bench workload is synthetic KV tensors: this test proves LMCache L2
# persistence only (level 2 of 3). It does NOT prove that vLLM + MiMo-V2.6-Pro
# + MAX_NUM_SEQS=2 reuses the cache for an existing Operit chat; that is the
# separate full-vLLM acceptance test (scripts/test-recovery.sh, level 3).

_ROUNDTRIP_START = 7            # deterministic workload identity (seq_no)
_ROUNDTRIP_NUM_TOKENS = 63      # + 1 seq token = 64 tokens = 4 chunks of 16
_ROUNDTRIP_SHAPE = "(2,64,16,8,128):float16:4"   # 4 layers, kv_size=2, fp16


def _bench_cmd(http_port: int, zmq_port: int, seq: int) -> list[str]:
    """Build the pinned CLI's server-bench invocation for one request."""
    return [
        LMBIN, "bench", "server",
        "--mode", "cpu", "--transfer-mode", "engine_driven",
        "--rpc-url", "tcp://127.0.0.1:%d" % zmq_port,
        "--url", "http://127.0.0.1:%d" % http_port,
        "--kvcache-shape-spec", _ROUNDTRIP_SHAPE,
        "--num-tokens", str(_ROUNDTRIP_NUM_TOKENS),
        "--start", str(seq), "--end", str(seq + 1),
        "--interval", "0",
    ]


def _run_bench(http_port: int, zmq_port: int, seq: int,
               log: Path, label: str, timeout: float = 300.0) -> str:
    """Run one `lmcache bench server` request; capture output to log and return it."""
    cmd = _bench_cmd(http_port, zmq_port, seq)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.open("ab").write(("=== bench %s: TIMEOUT ===\n" % label).encode())
        raise
    out = (proc.stdout or "") + (proc.stderr or "")
    log.open("ab").write(("\n=== bench %s rc=%d ===\n%s\n" % (label, proc.returncode, out)).encode())
    if proc.returncode != 0:
        raise AssertionError(
            "bench '%s' failed rc=%d:\n%s" % (label, proc.returncode, out[-3000:]))
    return out


def _parse_bench(out: str) -> dict:
    """Parse the pinned tool's own evidence lines (no invented fields).

    LOOKUP:        `  [seq N/cold] LOOKUP: <hit>/<total> chunks hit (X ms)`
    STORE:         `  [seq N/cold] STORE: stored (T tokens, ...)` -- present
                   only when miss_chunks > 0 (i.e. NOT a pure retrieval).
    RETRIEVE:      `  [seq N/cold] RETRIEVE: retrieved (T tokens, ...)`
    CHECKSUM:      `  [seq N/cold] CHECKSUM: <md5-16> (K chunks)`

    One bench invocation runs BOTH a cold and a warm pass per request, so
    `checksums` is [cold_digest, warm_digest]. The warm digest is computed
    AFTER the client zero-fills its buffers and RETRIEVE writes the data back
    (helpers._compute_client_checksums), so warm==ground_truth proves a lossless
    round trip; a silent no-op retrieve would hash as zeros and mismatch."""
    hits = [(int(a), int(b)) for a, b in
            re.findall(r"LOOKUP: (\d+)/(\d+) chunks hit", out)]
    checksums = [d for d, _n in re.findall(r"CHECKSUM: ([0-9a-f]+) \((\d+) chunks\)", out)]
    stored = len(re.findall(r"STORE: stored", out))
    retrieved = len(re.findall(r"RETRIEVE: retrieved", out))
    return {"hits": hits, "checksums": checksums,
            "stored": stored, "retrieved": retrieved}


def _wait_ready(proc: subprocess.Popen, http_port: int, log: Path,
                deadline_s: float = 180.0) -> None:
    """Real readiness check: HTTP liveness/health endpoint + process alive.

    The pinned 0.5.3 HTTP API serves GET /  -> {"status":"ok"} (liveness) and
    GET /healthcheck -> 200 {"status":"healthy"} once the cache engine is up.
    A listening TCP port alone or a bare sleep is NOT accepted as readiness."""
    import urllib.error
    import urllib.request
    end = time.time() + deadline_s
    last = "no attempt"
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(
                "lmcache server exited during startup (rc=%s); log tail:\n%s"
                % (proc.returncode,
                   log.read_text()[-2000:] if log.exists() else "(no log)"))
        for path in ("/healthcheck", "/"):
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d%s" % (http_port, path), timeout=3) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError) as e:
                last = "GET %s: %s" % (path, e)
        time.sleep(1)
    raise RuntimeError(
        "lmcache server not ready in %ss (last: %s); log tail:\n%s"
        % (deadline_s, last,
           log.read_text()[-2000:] if log.exists() else "(no log)"))


def _start_server(adapter: Path, zmq_port: int, http_port: int,
                  log: Path, phase: str) -> subprocess.Popen:
    """Start a real lmcache server subprocess; stdout+stderr go to `log`.

    The L2 data directory is NEVER wiped between phases -- the same directory
    is reused across the restart by design (that is the whole point)."""
    cmd = [
        LMBIN, "server",
        "--host", "127.0.0.1", "--port", str(zmq_port),
        "--l1-size-gb", "1", "--chunk-size", "16",
        "--eviction-policy", "LRU",
        "--eviction-trigger-watermark", "0.9", "--eviction-ratio", "0.1",
        "--l2-adapter", _adapter_json_string(adapter),
        "--http-host", "127.0.0.1", "--http-port", str(http_port),
    ]
    f = open(log, "ab")
    f.write(("\n=== %s: %s ===\n" % (phase, " ".join(cmd))).encode())
    f.flush()
    return subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)


def _stop_server(proc: subprocess.Popen, log: Path, label: str) -> None:
    """Terminate a server process completely: TERM -> wait -> KILL.

    The old process must be dead before the next phase starts; a still-serving
    instance would make the post-restart read meaningless."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        log.open("ab").write(
            ("=== %s: terminate timed out, killing ===\n" % label).encode())
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.open("ab").write(
                ("=== %s: kill did not reap process ===\n" % label).encode())
    assert proc.poll() is not None, \
        "%s: server process still alive after terminate+kill" % label


def _l2_data_files(l2: Path) -> list[Path]:
    """Real cache data files written by the nixl_store_dynamic adapter.

    The adapter derives `safe_model_name_kvrank_groupid_chunkhash.bin`
    (nixl_store_dynamic_l2_adapter._object_key_to_filename), so data files are
    *.bin under the rendered cache root. Excludes any test bookkeeping files."""
    return sorted(p for p in l2.glob("*.bin") if p.is_file() and p.stat().st_size > 0)


@pytest.mark.skipif(
    os.environ.get("KVP_LIVE_LMCACHE") != "1",
    reason="real LMCache store/retrieve round-trip needs the pinned lmcache "
           "package (0.5.3) and a POSIX-SHM Linux host: set KVP_LIVE_LMCACHE=1 "
           "to run it (documented external prerequisite, see docs/benchmarks.md)",
)
def test_lmcache_store_restart_retrieve_roundtrip(tmp_path):
    """Level 2: real LMCache persistence round-trip (synthetic KV, no vLLM).

    Store -> STOP (process fully terminated) -> start a FRESH server process on
    the SAME L2 directory -> retrieve the PRE-restart objects -> compare chunk
    checksums. A negative control over an empty L2 directory proves the tool
    does not report a hit on absent data. Nothing is re-stored after restart:
    the post-restart run must hit `LOOKUP: N/N` with ZERO `STORE: stored` lines.

    FAIL taxonomy (distinct, actionable messages):
      1. server startup failure       -> _wait_ready RuntimeError (log tail)
      2. adapter/configuration error  -> startup/first-bench failures
      3. store failure                -> no `STORE: stored` / no L2 files
      4. failure to stop old server   -> _stop_server assertion
      5. failure to start new server  -> _wait_ready RuntimeError (log tail)
      6. cache miss after restart     -> `LOOKUP: 0/N` on the fresh server
      7. retrieve failure             -> no `RETRIEVE: retrieved` line
      8. payload/checksum mismatch    -> digest differs from Phase C
    """
    # ---------------- Phase A: isolated environment ----------------------
    l2 = tmp_path / "l2"
    adapter = _render_adapter(tmp_path, budget_gb="1")
    log = tmp_path / "roundtrip.log"
    zmq1, http1 = _free_port(), _free_port()
    zmq2, http2 = _free_port(), _free_port()
    l2_files_before: list[Path] = []

    server1 = None
    server2 = None
    try:
        # ---------------- Phase B: start a real server --------------------
        server1 = _start_server(adapter, zmq1, http1, log, "phase-B/first")
        try:
            _wait_ready(server1, http1, log)
        except RuntimeError as e:
            pytest.fail("[1/2 server startup] %s" % e)

        # ---------------- Phase C: store through the real data path ------
        out_cold = _run_bench(http1, zmq1, _ROUNDTRIP_START, log, "phase-C/store")
        cold = _parse_bench(out_cold)
        if cold["stored"] < 1:
            pytest.fail(
                "[3/8 store failure] bench reported no successful STORE on the "
                "first server. Output:\n%s" % out_cold[-3000:])
        if not cold["checksums"]:
            pytest.fail(
                "[8/8 checksum] bench printed no ground-truth CHECKSUM for the "
                "stored workload. Output:\n%s" % out_cold[-3000:])
        # multiple chunks per request prove the non-trivial multi-chunk case
        hit_c, total_c = cold["hits"][0]
        if total_c < 2:
            pytest.fail(
                "workload must span >=2 chunks for a meaningful round-trip, "
                "got %d (chunk_size=16, num_tokens=%d)"
                % (total_c, _ROUNDTRIP_NUM_TOKENS))
        ground_checksum = cold["checksums"][0]

        # Confirm LMCache actually wrote cache objects to the L2 directory.
        # (Store is asynchronous -- poll the adapter's own output files.)
        for _ in range(60):
            l2_files_before = _l2_data_files(l2)
            if l2_files_before:
                break
            time.sleep(1)
        if not l2_files_before:
            pytest.fail(
                "[3/8 store failure] no persistent L2 data files (*.bin) "
                "appeared under %s after STORE -- the adapter did not persist "
                "the workload to disk" % l2)

        # ---------------- Phase D: stop the server completely ------------
        _stop_server(server1, log, "phase-D/first-server")
        server1 = None
        # Prove nothing re-creates the payload between phases: no bench and no
        # server process is left running here (only this test process).

        # ---------------- Phase E: start a FRESH server process ----------
        l2_files_kept = _l2_data_files(l2)
        assert l2_files_kept == l2_files_before, \
            "L2 data files changed while no server was running " \
            "(%d before, %d now)" % (len(l2_files_before), len(l2_files_kept))
        server2 = _start_server(adapter, zmq2, http2, log, "phase-E/second")
        try:
            _wait_ready(server2, http2, log)
        except RuntimeError as e:
            pytest.fail("[5/8 new server startup] %s" % e)
        assert server2.pid != (server1.pid if server1 else -1), \
            "second server must be a NEW process"

        # ---------------- Phase F: retrieve the PRE-restart objects ------
        out_after = _run_bench(http2, zmq2, _ROUNDTRIP_START, log, "phase-F/retrieve")
        after = _parse_bench(out_after)

        if after["stored"] != 0:
            pytest.fail(
                "[8/8 payload] post-restart run performed %d new STORE ops -- "
                "a successful read after re-storing proves NOTHING about "
                "recovery. The retrieve-only pass must not store.\n%s"
                % (after["stored"], out_after[-3000:]))

        if not after["hits"]:
            pytest.fail(
                "[6/8 cache miss] post-restart bench printed no LOOKUP result"
                "\n%s" % out_after[-3000:])
        hit_a, total_a = after["hits"][0]
        if hit_a != total_a or total_a == 0:
            pytest.fail(
                "[6/8 cache miss after restart] fresh server found only "
                "%d/%d chunks (pre-restart store covered %d/%d, %d L2 files "
                "kept) -- the objects were not recovered from persistent L2\n%s"
                % (hit_a, total_a, hit_c, total_c, len(l2_files_kept),
                   out_after[-3000:]))

        if after["retrieved"] < 1:
            pytest.fail(
                "[7/8 retrieve failure] LOOKUP hit %d/%d chunks but no "
                "RETRIEVE succeeded\n%s"
                % (hit_a, total_a, out_after[-3000:]))

        if not after["checksums"]:
            pytest.fail(
                "[8/8 payload] no post-restart CHECKSUM was produced -- "
                "cannot compare recovered bytes against the originals\n%s"
                % out_after[-3000:])
        # The post-restart digest is the WARM-pass hash: the client zero-filled
        # its buffers and RETRIEVE repopulated them from the fresh server, so
        # this digest being equal to the pre-restart ground truth proves the
        # exact bytes came back from persistent L2 (a no-op retrieve would
        # hash zeros and mismatch).
        recovered_checksum = after["checksums"][-1]
        if recovered_checksum != ground_checksum:
            pytest.fail(
                "[8/8 payload/checksum mismatch] the bytes recovered after "
                "the restart differ from the bytes stored before it:\n"
                "  pre-restart : %s\n  post-restart: %s\n%s"
                % (ground_checksum, recovered_checksum, out_after[-3000:]))

        # ---------------- Negative control: empty L2 dir -----------------
        # Same workload identity, but against a FRESH server whose L2 dir has
        # never seen the data. The bench must report a MISS (0/N chunks),
        # otherwise its "hit" signal cannot be trusted as recovery evidence.
        nc_root = tmp_path / "negative-control"
        nc_root.mkdir(parents=True, exist_ok=True)
        nc_l2 = nc_root / "l2"
        nc_adapter = _render_adapter(nc_root, budget_gb="1")
        nc_zmq, nc_http = _free_port(), _free_port()
        nc_server = _start_server(nc_adapter, nc_zmq, nc_http, log,
                                  "phase-G/negative-control")
        try:
            _wait_ready(nc_server, nc_http, log)
            out_nc = _run_bench(nc_http, nc_zmq, _ROUNDTRIP_START,
                               log, "phase-G/negative-control")
            nc = _parse_bench(out_nc)
            if nc["hits"] and nc["hits"][0][0] != 0:
                pytest.fail(
                    "[negative control] bench reported %d/%d chunks hit on an "
                    "EMPTY L2 directory -- its hit signal is not trustworthy "
                    "as recovery evidence\n%s"
                    % (nc["hits"][0][0], nc["hits"][0][1], out_nc[-3000:]))
        finally:
            _stop_server(nc_server, log, "phase-G/negative-control")

        # ---------------- PASS: level-2 evidence complete ----------------
        print(
            "\n[level 2 PASS] LMCache L2 persistence round-trip verified:\n"
            "  pre-restart : cold pass %d/%d chunks hit -> STORE ok, "
            "checksum %s, %d L2 data files\n"
            "  post-restart: cold pass %d/%d chunks hit (0 stores), "
            "checksum %s (match)\n"
            "  negative ctl: empty L2 -> %s chunks hit (must be 0)\n"
            "  NOTE: synthetic KV via `lmcache bench server` (level 2 of 3). "
            "The vLLM + MiMo-V2.6-Pro scenario is NOT covered here -- run "
            "scripts/test-recovery.sh (level 3) for that."
            % (hit_c, total_c, ground_checksum, len(l2_files_before),
               hit_a, total_a, recovered_checksum,
               nc["hits"][0][0] if nc["hits"] else "?"))
    finally:
        # Guaranteed cleanup: no stale server processes survive a failed test.
        if server1 is not None:
            _stop_server(server1, log, "cleanup/server1")
        if server2 is not None:
            _stop_server(server2, log, "cleanup/server2")