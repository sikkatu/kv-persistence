"""Round 49: PATH auto, monitoring first, final-report without round history.

User findings:
* `export PATH="$HOME/.venvs/lmcache/bin:$PATH"` was still required by hand --
  the scripts must resolve the venv themselves (env.sh prepends it);
* "Optional: Prometheus + Grafana monitoring" was wrong -- monitoring starts
  FIRST and the cache auto-attaches to it;
* final-report must NOT carry per-round fix texts (they live in CHANGELOG).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


# ---------------------------------------------------------------- PATH auto
def test_env_prepends_venv_bin_to_path():
    t = _read("env.sh")
    assert "_kvp_venv_bin(){" in t, "env.sh must resolve the lmcache venv bin"
    assert '.venvs/lmcache/bin' in t, "the standard venv location must be tried"
    assert 'PATH="$_kvp_venv_bin:$PATH"' in t, "the venv bin must be prepended to PATH"
    assert "export PATH" in t, "the PATH change must be exported"


def test_no_manual_path_export_needed_anywhere():
    """No instruction may tell the operator to export PATH by hand."""
    for name in ("README.md", "README.ru.md", "README.zh.md"):
        t = (ROOT / name).read_text()
        for line in t.splitlines():
            assert "export PATH=" not in line, (
                "%s still instructs a manual PATH export: %r" % (name, line))
    for name in ("Quick-Start.md", "Quick-Start.ru.md", "Quick-Start.zh.md"):
        t = (ROOT.parent / "kvp.wiki" / name)
        if not t.exists():
            continue
        for line in t.read_text().splitlines():
            assert line.strip().startswith("export PATH=") is False, (
                "%s still instructs a manual PATH export: %r" % (name, line))


def test_host_version_probe_uses_venv_python():
    """The system python3 has no lmcache -- the version pre-flight must use
    the venv python (_kvp_python)."""
    t = _read("run-vllm-with-persist.sh")
    assert '"$(_kvp_python)" -c' in t, (
        "the host lmcache version must be read via the venv python")
    # the old broken form must be gone
    assert '_host_lmcache="$(python3 -c' not in t


# ---------------------------------------------------------------- monitoring first
def test_quick_start_starts_monitoring_first():
    for name in ("README.md", "README.ru.md", "README.zh.md"):
        t = (ROOT / name).read_text()
        obs = t.find("start-observability.sh")
        cache = t.find("start-cache.sh")
        # find them inside the Quick start command block
        assert obs > 0 and cache > 0, name
        assert obs < cache, (
            "%s: monitoring must start BEFORE the cache (auto-attach)" % name)


def test_wiki_step6_is_monitoring_then_cache():
    for name in ("Quick-Start.md", "Quick-Start.ru.md", "Quick-Start.zh.md"):
        p = ROOT.parent / "kvp.wiki" / name
        if not p.exists():
            continue
        t = p.read_text()
        i6 = t.find("## 6.")
        i7 = t.find("## 7.")
        assert i6 >= 0 and i7 > i6, name
        sec = t[i6:i7]
        obs = sec.find("start-observability.sh")
        cache = sec.find("start-cache.sh")
        assert 0 <= obs < cache, (
            "%s step 6: monitoring must come before start-cache" % name)


# ---------------------------------------------------------------- final-report cleanup
def test_final_report_has_no_round_history():
    """Per-round texts live in CHANGELOG -- the final report keeps only the
    verified status and technical record."""
    import re
    for name in ("final-report.md", "final-report.ru.md", "final-report.zh.md"):
        t = (ROOT / "docs" / name).read_text()
        for rx in (r"^## Review round \d+", r"^## Правки по итогам",
                   r"^## 第 \d+ 轮"):
            assert not re.search(rx, t, re.M), (
                "%s still has a per-round section (%s)" % (name, rx))
        assert "CHANGELOG" in t, "%s must point readers to the CHANGELOG" % name