"""Protocol-level ping of the LMCache MP server (round 44).

Why: the TCP-reachability check in the engine launcher (start-all.sh) proves only
that a socket is open. The real handshake (register_kv_caches) goes over
the lmcache RPC protocol, and the install run kept failing THERE (300s
timeout) while the TCP check passed. This module speaks the actual
protocol: it builds the same RequestClient the vLLM connector uses and
issues ``ping`` -- if the server answers, the RPC channel is healthy; if
it does not, the launch is refused BEFORE the 13-minute model load.

Usage (the pre-flight calls exactly this):
    PYTHONPATH=<repo>/implementation python3 -m kvpersist.mp_ping HOST PORT [TIMEOUT]
Exit codes: 0 = pong, 2 = no pong (timeout/protocol error), 3 = lmcache
not importable (the check is skipped -- not a failure).
"""
from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python3 -m kvpersist.mp_ping HOST PORT [TIMEOUT]", file=sys.stderr)
        return 2
    host = sys.argv[1]
    port = sys.argv[2]
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    url = f"tcp://{host}:{port}"
    try:
        from lmcache.v1.multiprocess.transport.factory import RequestClientFactory
    except Exception as exc:  # noqa: BLE001 - slim install / no lmcache here
        print(f"mp_ping: lmcache not importable ({exc.__class__.__name__}) -- skipped")
        return 3
    try:
        client = RequestClientFactory.create(url)
    except Exception as exc:  # noqa: BLE001
        print(f"mp_ping: FAIL -- cannot create client for {url}: {exc}")
        return 2
    try:
        # ping(None) is the heartbeat probe: it needs NO prior registration
        # (register_kv_caches would be the thing that hangs).
        pong = client.ping(None).result(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - timeout / protocol error
        print(
            f"mp_ping: FAIL -- no pong from {url} within {timeout}s "
            f"({exc.__class__.__name__}: {exc})"
        )
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        return 2
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass
    print(f"mp_ping: OK -- server at {url} answered the RPC ping (pong={pong})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())