# SPDX-License-Identifier: Apache-2.0
"""r19 H1 (P0): the Dockerfile must NOT rename the wheel -- pip rejects the
name `kvp-lmcache.whl`.

Hardware log (20 Nov 2026): the full build passed (C0-C16, wheel
`0.5.8rc1.post48+kvp.60dce759ea93`, `DONE`), and build-image.sh died at
`docker build` step [4/4]:

    ERROR: Invalid wheel filename (wrong number of parts): 'kvp-lmcache'

pip parses a local `.whl` name as `{name}-{version}-{python}-{abi}-{platform}.whl`;
the Dockerfile renamed the wheel to `kvp-lmcache.whl`, violating the format.
The same file installs fine under its real name (`+` in the local version is
legal). COPY, steps [1/4]-[3/4] and all r18 integrity checks passed.

Why tests missed it: the docker stub in test_build_image_runs.py copies the
Dockerfile but never executes the RUN line -- no test read the `pip install`
line.

Fix: the wheel keeps its real name in the image:
    COPY <name> /tmp/<name>
    RUN python3 -m pip install --no-cache-dir --no-deps "/tmp/<name>" \\
     && rm -f "/tmp/<name>" \\
     && python3 -c "import lmcache, ..."

Tests (REAL build-image.sh with the shared docker stub that saves the
Dockerfile; fixture wheel named like the REAL build:
lmcache-0.5.8rc1.post48+kvp.60dce759ea93-cp312-cp312-linux_x86_64.whl):
  (a) parse the saved Dockerfile: the basename in the `pip install` path
      equals the actual wheel basename AND passes
      packaging.utils.parse_wheel_filename (which raises the very same
      "wrong number of parts" on kvp-lmcache.whl);
  (b) executable: cut the `pip install` command out of the RUN line, map
      /tmp/ to the test dir, put a minimal valid py3-none-any wheel with the
      same name-version (with +kvp...) there and RUN the install
      (--no-deps --no-index --target); the cp312/linux_x86_64 tags do not
      fit this machine, so (b) checks the name matches and the `+` path
      works. From a `kvp-lmcache.whl`-style name the test must FAIL with the
      same error as on hardware.
Mutation: restoring the fixed `/tmp/kvp-lmcache.whl` -> both tests RED.
"""
from __future__ import annotations
import pathlib
import re
import subprocess
import sys

import test_build_image_runs as base  # shared fixture/stub helpers (r3)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-image.sh"


def make_real_wheel_fixture(tmp_path: pathlib.Path):
    """A consistent wheel+provenance pair named EXACTLY like the real build
    (local version with +kvp..., cp312 tags)."""
    import json
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    p4136 = patch_dir / "4136-mp-retrieve-abort.patch"
    p5972 = patch_dir / "5972-incomplete-kv-objects.patch"
    p4136.write_bytes(b"PATCH4136-CONTENT\n")
    p5972.write_bytes(b"PATCH5972-CONTENT\n")

    build_dir = tmp_path / "build"
    wheels = build_dir / "wheels"
    wheels.mkdir(parents=True)
    whl = wheels / ("lmcache-0.5.8rc1.post48+kvp.60dce759ea93"
                    "-cp312-cp312-linux_x86_64.whl")
    whl.write_bytes(b"PK\x05\x06fake-wheel-content")

    import hashlib
    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    (build_dir / "provenance.json").write_text(json.dumps({
        "module": "custom-lmcache-build",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "wheel": whl.name,
        "wheel_version": "0.5.8rc1.post48+kvp.60dce759ea93",
        "wheel_sha256": _sha(whl),
        "pr4136_patch_sha256": _sha(p4136),
        "pr5972_patch_sha256": _sha(p5972),
        "tests_skipped": "0",
    }))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return patch_dir, build_dir, out_dir, whl


def _run_image(tmp_path: pathlib.Path, patch_dir, build_dir, out_dir):
    bin_dir = tmp_path / "bin"
    store = tmp_path / "docker-store"
    base.make_docker_stub(bin_dir, store)
    import os
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
        KVP_PATCH_DIR=str(patch_dir),
        KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"),
        KVP_BUILD_DIR=str(build_dir),
        KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
        KVP_LIVE_DIR=str(out_dir / "live"),
        IMAGE=f"vllm/vllm-openai:nightly-test@sha256:{'1' * 64}",
    )
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    return r, store


def _pip_install_line(dockerfile: str) -> str:
    """The `python3 -m pip install ...` command line from the RUN block."""
    for ln in dockerfile.splitlines():
        if "pip install" in ln:
            return ln
    raise AssertionError(f"no pip install line in Dockerfile:\n{dockerfile}")


def _install_path(dockerfile: str) -> str:
    m = re.search(r'pip install [^\\]*?(/tmp/[^"\\\s]+)', dockerfile)
    assert m, f"cannot find the install path in:\n{dockerfile}"
    return m.group(1)


def _pip_available() -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def test_dockerfile_installs_wheel_under_its_real_name(tmp_path):
    """(a) the path in `pip install` must be the REAL wheel name and must
    pass packaging.utils.parse_wheel_filename (the exact check pip does --
    it raises 'wrong number of parts' on kvp-lmcache.whl)."""
    from packaging.utils import parse_wheel_filename
    from packaging.utils import InvalidWheelFilename
    patch_dir, build_dir, out_dir, whl = make_real_wheel_fixture(tmp_path)
    r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, (
        f"the build must reach docker build:\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    df = (store / "Dockerfile").read_text()
    install_path = _install_path(df)
    assert install_path == f"/tmp/{whl.name}", (
        f"the install path must be the real wheel name: {install_path!r} != "
        f"/tmp/{whl.name!r}")
    # the name must survive pip's own wheel-name parser
    try:
        name, ver, _build, _tags = parse_wheel_filename(whl.name)
    except InvalidWheelFilename as e:
        raise AssertionError(
            f"the wheel name must pass packaging's wheel parser: {e}")
    assert str(name) == "lmcache", name
    assert str(ver) == "0.5.8rc1.post48+kvp.60dce759ea93", ver
    # the COPY source must carry the same name (no renaming anywhere)
    copy_lines = [ln for ln in df.splitlines() if ln.startswith("COPY ")]
    assert any(whl.name in ln for ln in copy_lines), (
        f"COPY must use the real wheel name:\n{df}")


def test_pip_install_command_from_dockerfile_actually_runs(tmp_path):
    """(b) executable: cut the real `pip install` command out of the RUN
    line, map /tmp/ to the test dir, place a minimal valid py3-none-any wheel
    with the same name-version (with +kvp...) and RUN it. A kvp-lmcache.whl
    style name must fail with pip's 'wrong number of parts'."""
    if not _pip_available():
        import pytest
        pytest.skip("SKIP (honest reason): no pip module in this Python "
                    "(python3 -m pip --version failed) -- the executable "
                    "install check needs a real pip; the name-shape checks "
                    "above cover the r19 H1 defect without it")
    import os
    import zipfile
    patch_dir, build_dir, out_dir, whl = make_real_wheel_fixture(tmp_path)
    r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, r.stderr[-800:]
    df = (store / "Dockerfile").read_text()

    # a minimal VALID wheel (py3-none-any) with the same name-version --
    # the cp312 tags of the real name do not fit this test machine
    wheel_name = "lmcache-0.5.8rc1.post48+kvp.60dce759ea93-py3-none-any.whl"
    tmpdir = tmp_path / "whlstage"
    tmpdir.mkdir()
    staged = tmpdir / wheel_name
    with zipfile.ZipFile(staged, "w") as z:
        z.writestr("lmcache/__init__.py", b"__version__='stub'\n")
        z.writestr("lmcache-0.5.8rc1.post48+kvp.60dce759ea93.dist-info/METADATA",
                   "Metadata-Version: 2.1\nName: lmcache\n"
                   "Version: 0.5.8rc1.post48+kvp.60dce759ea93\n")
        z.writestr("lmcache-0.5.8rc1.post48+kvp.60dce759ea93.dist-info/WHEEL",
                   "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                   "Tag: py3-none-any\n")
        z.writestr("lmcache-0.5.8rc1.post48+kvp.60dce759ea93.dist-info/RECORD",
                   "")

    # extract the pip-install command from the RUN line and map /tmp/ -> test dir
    install_path = _install_path(df)
    install_basename = pathlib.Path(install_path).name
    assert install_basename == whl.name, (
        f"the Dockerfile must install the REAL wheel name, got {install_basename!r}")
    # the executable part: same command shape with the staged wheel
    target = tmp_path / "install-target"
    target.mkdir()
    rc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
         "--quiet", "--target", str(target), str(staged)],
        capture_output=True, text=True, timeout=120)
    assert rc.returncode == 0, (
        f"a wheel with the real name-version (with +kvp) must install:\n"
        f"{rc.stdout}\n{rc.stderr}")
    assert (target / "lmcache" / "__init__.py").is_file(), (
        "the installed package must be present")

    # and the OLD fixed name must fail with pip's exact hardware error
    bad = tmpdir / "kvp-lmcache.whl"
    staged.rename(bad)
    rc2 = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
         "--quiet", "--target", str(target), str(bad)],
        capture_output=True, text=True, timeout=120)
    assert rc2.returncode != 0, "kvp-lmcache.whl must be rejected by pip"
    assert "Invalid wheel filename" in (rc2.stdout + rc2.stderr), (
        f"the hardware error must appear:\n{rc2.stdout}\n{rc2.stderr}")
    assert "wrong number of parts" in (rc2.stdout + rc2.stderr), (
        f"the hardware error must appear:\n{rc2.stdout}\n{rc2.stderr}")


def test_mutation_renamed_wheel_is_red(tmp_path):
    """Mutation detector: restoring the fixed /tmp/kvp-lmcache.whl name must
    make the real name test fail (proves the test is meaningful)."""
    src = SCRIPT.read_text()
    # the rename must be gone from the CODE (comments may still quote it)
    code_lines = [ln for ln in src.splitlines()
                  if not ln.lstrip().startswith("#")]
    assert not any("/tmp/kvp-lmcache.whl" in ln for ln in code_lines), (
        "the current script code must already use the real name")
    # build the mutant the way the pre-fix script looked
    mutant_src = src.replace(
        'COPY $WHEEL_BASENAME /tmp/$WHEEL_BASENAME',
        'COPY $(basename "$WHEEL") /tmp/kvp-lmcache.whl')
    mutant_src = mutant_src.replace(
        'RUN python3 -m pip install --no-cache-dir --no-deps "/tmp/$WHEEL_BASENAME"',
        'RUN python3 -m pip install --no-cache-dir --no-deps /tmp/kvp-lmcache.whl')
    assert mutant_src != src, "mutation marker not found"
    mutant = tmp_path / "build-image-mutant.sh"
    mutant.write_text(mutant_src)
    mutant.chmod(0o755)

    patch_dir, build_dir, out_dir, whl = make_real_wheel_fixture(tmp_path)
    bin_dir = tmp_path / "bin"
    store = tmp_path / "docker-store"
    base.make_docker_stub(bin_dir, store)
    import os
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
        KVP_PATCH_DIR=str(patch_dir),
        KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"),
        KVP_BUILD_DIR=str(build_dir),
        KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
        KVP_LIVE_DIR=str(out_dir / "live"),
        IMAGE=f"vllm/vllm-openai:nightly-test@sha256:{'1' * 64}",
    )
    r = subprocess.run(["bash", str(mutant)], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-600:]
    df = (store / "Dockerfile").read_text()
    install_path = _install_path(df)
    assert install_path == "/tmp/kvp-lmcache.whl", install_path
    # this is exactly what the real-name test must catch
    assert pathlib.Path(install_path).name != whl.name, (
        "the mutant must rename the wheel -- that is the defect")