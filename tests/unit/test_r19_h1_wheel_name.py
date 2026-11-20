# SPDX-License-Identifier: Apache-2.0
"""r19 H1 + r20 K4: the Dockerfile must install the wheel under its REAL
name, through ONE path (COPY src == COPY dst == pip install path == rm -f
path), with --no-deps, and the executable pip install must run the REAL
command from the Dockerfile.

r19 H1 (hardware, 20 Nov 2026): the Dockerfile renamed the wheel to
/tmp/kvp-lmcache.whl; pip parses a local `.whl` name as
{name}-{version}-{python}-{abi}-{platform}.whl and rejected it --
'ERROR: Invalid wheel filename (wrong number of parts): 'kvp-lmcache'' at
docker build step [4/4].

r20 K4 (the review's test holes): the old tests found the install path with
a regex and checked COPY only loosely -- renaming just COPY dst (or just
the rm path) passed green; the --no-deps flag was never checked; the
'executable' test ran a HAND-WRITTEN pip install for a DIFFERENT wheel; the
mutation detector never ran the tests on the mutant.

Tests (REAL build-image.sh with the shared docker stub that saves the
Dockerfile; fixture wheel named like the REAL build):
  (a) check_dockerfile(store): parses the generated Dockerfile with real
      structure (join `\\`-continuations, split RUN on &&, shlex.split) --
      COPY src == the real wheel basename; COPY dst == the pip install path
      == the rm -f path; the name passes packaging.utils.parse_wheel_filename;
      the pip install carries --no-deps;
  (b) executable: the REAL pip install command from the Dockerfile, with a
      minimal wheel carrying the REAL name and tags (cp312-cp312-linux_x86_64,
      Tag: in WHEEL matches), /tmp/ mapped to the test dir, plus
      --no-index --target --platform --python-version --implementation --abi
      --only-binary=:all:. A kvp-lmcache.whl name must fail with pip's exact
      hardware error;
  (c) mutation detectors: COPY dst only / pip path only / rm path only /
      --no-deps dropped -> each makes check_dockerfile report the violation
      (the tests (a)/(b) are RED on the mutants).
"""
from __future__ import annotations
import pathlib
import shlex
import shutil
import subprocess
import sys

import pytest

import test_build_image_runs as base  # shared fixture/stub helpers (r3)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-image.sh"


def _sha256(path: pathlib.Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


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

    (build_dir / "provenance.json").write_text(json.dumps({
        "module": "custom-lmcache-build",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "wheel": whl.name,
        "wheel_version": "0.5.8rc1.post48+kvp.60dce759ea93",
        "wheel_sha256": _sha256(whl),
        "pr4136_patch_sha256": _sha256(p4136),
        "pr5972_patch_sha256": _sha256(p5972),
        "tests_skipped": "0",
    }))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return patch_dir, build_dir, out_dir, whl


def _run_image(tmp_path: pathlib.Path, patch_dir, build_dir, out_dir,
               script: pathlib.Path = SCRIPT):
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
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    return r, store


def _logical_lines(dockerfile: str) -> list[str]:
    """Join `\\`-continuations into logical shell lines."""
    out = []
    buf = ""
    for ln in dockerfile.splitlines():
        s = ln.rstrip()
        if s.endswith("\\"):
            buf += s[:-1] + " "
        else:
            buf += s
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def check_dockerfile(store: pathlib.Path, wheel_basename: str) -> list[str]:
    """The ONE structural check of the generated Dockerfile (r20 K4):
    COPY src == wheel basename; COPY dst == pip install path == rm -f path;
    the name passes packaging.utils.parse_wheel_filename; the pip install
    carries --no-deps. Returns a list of violations (empty = clean)."""
    from packaging.utils import parse_wheel_filename, InvalidWheelFilename
    df = (store / "Dockerfile").read_text()
    viol = []
    lines = _logical_lines(df)

    # -- COPY: src must be the real wheel basename (no rename anywhere)
    copy_src = copy_dst = None
    for ln in lines:
        if ln.startswith("COPY ") and wheel_basename in ln:
            parts = shlex.split(ln)
            # COPY <src> <dst>
            if len(parts) >= 3:
                copy_src, copy_dst = parts[1], parts[2]
    if copy_src is None:
        viol.append(f"COPY does not use the real wheel name {wheel_basename!r}")
    elif pathlib.Path(copy_src).name != wheel_basename:
        viol.append(f"COPY src {copy_src!r} != wheel basename {wheel_basename!r}")

    # -- RUN: split on &&, shlex.split each part
    run_parts = []
    for ln in lines:
        if ln.startswith("RUN "):
            body = ln[4:]
            for part in body.split("&&"):
                part = part.strip()
                if part:
                    run_parts.append(shlex.split(part))

    install_path = rm_path = None
    for argv in run_parts:
        if not argv:
            continue
        if "pip" in argv and "install" in argv:
            # the positional arg after the flags is the wheel path
            i = argv.index("install")
            pos = [a for a in argv[i + 1:] if not a.startswith("-")]
            if pos:
                install_path = pos[0]
            if "--no-deps" not in argv:
                viol.append("pip install lacks --no-deps (pip would pull "
                            "dependencies into the base image)")
        if "rm" in argv and "-f" in argv:
            pos = [a for a in argv[2:] if not a.startswith("-")]
            if pos:
                rm_path = pos[0]

    if install_path is None:
        viol.append("no pip install command in the Dockerfile RUN block")
    elif pathlib.Path(install_path).name != wheel_basename:
        viol.append(f"pip install path {install_path!r} != wheel basename "
                    f"{wheel_basename!r}")
    if rm_path is None:
        viol.append("no rm -f command in the Dockerfile RUN block")
    elif rm_path != install_path:
        viol.append(f"rm -f path {rm_path!r} != pip install path {install_path!r}")
    if copy_dst is not None and install_path is not None and copy_dst != install_path:
        viol.append(f"COPY dst {copy_dst!r} != pip install path {install_path!r}")

    # -- the name must pass pip's own wheel-name parser
    try:
        name, ver, _build, _tags = parse_wheel_filename(wheel_basename)
    except InvalidWheelFilename as e:
        viol.append(f"wheel name {wheel_basename!r} fails packaging's parser: {e}")
    return viol


def _pip_available() -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# (a) structural check of the generated Dockerfile
# ---------------------------------------------------------------------------
def test_dockerfile_single_path_and_no_deps(tmp_path):
    """(a) check_dockerfile on the REAL generated Dockerfile: COPY src ==
    wheel basename; COPY dst == pip install path == rm -f path; the name
    passes packaging.utils.parse_wheel_filename; --no-deps present."""
    patch_dir, build_dir, out_dir, whl = make_real_wheel_fixture(tmp_path)
    r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, (
        f"the build must reach docker build:\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    viol = check_dockerfile(store, whl.name)
    assert not viol, "Dockerfile violations:\n" + "\n".join(viol)


# ---------------------------------------------------------------------------
# (b) executable: the REAL pip install command from the Dockerfile
# ---------------------------------------------------------------------------
def test_pip_install_command_from_dockerfile_actually_runs(tmp_path):
    """(b) run the REAL `pip install` command cut out of the Dockerfile RUN
    line (a minimal wheel with the REAL name and cp312 tags; /tmp/ mapped to
    the test dir; --no-index --target --platform ... added). A
    kvp-lmcache.whl-style name must fail with pip's exact hardware error."""
    if not _pip_available():
        pytest.skip("SKIP (honest reason): no pip module in this Python "
                    "(python3 -m pip --version failed) -- the executable "
                    "install check needs a real pip; check_dockerfile "
                    "covers the r19 H1 defect without it")
    import zipfile
    patch_dir, build_dir, out_dir, whl = make_real_wheel_fixture(tmp_path)
    r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, r.stderr[-800:]
    df = (store / "Dockerfile").read_text()

    # a minimal VALID wheel with the REAL name and tags
    wheel_name = ("lmcache-0.5.8rc1.post48+kvp.60dce759ea93"
                  "-cp312-cp312-linux_x86_64.whl")
    tmpdir = tmp_path / "whlstage"
    tmpdir.mkdir()
    staged = tmpdir / wheel_name
    with zipfile.ZipFile(staged, "w") as z:
        z.writestr("lmcache/__init__.py", b"__version__='stub'\n")
        z.writestr(
            "lmcache-0.5.8rc1.post48+kvp.60dce759ea93.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: lmcache\n"
            "Version: 0.5.8rc1.post48+kvp.60dce759ea93\n")
        z.writestr(
            "lmcache-0.5.8rc1.post48+kvp.60dce759ea93.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: cp312-cp312-linux_x86_64\n")
        z.writestr(
            "lmcache-0.5.8rc1.post48+kvp.60dce759ea93.dist-info/RECORD", "")

    # -- cut the REAL pip install command out of the RUN line
    install_argv = None
    for ln in _logical_lines(df):
        if ln.startswith("RUN "):
            for part in ln[4:].split("&&"):
                toks = shlex.split(part.strip())
                if "pip" in toks and "install" in toks:
                    install_argv = toks
                    break
        if install_argv:
            break
    assert install_argv, f"no pip install in the Dockerfile:\n{df}"

    # map /tmp/ -> the staging dir and add the platform/strictness flags the
    # review specifies (the cp312 tags do not fit every test machine)
    mapped = []
    for tok in install_argv:
        if tok.startswith("/tmp/"):
            mapped.append(str(tmpdir / pathlib.Path(tok).name))
        else:
            mapped.append(tok)
    # drop the leading interpreter tokens (python3 -m pip install ...) --
    # the command is executed with sys.executable below
    while mapped and mapped[0] in ("python3", "-m", "pip", "install"):
        mapped.pop(0)
    cmd = [sys.executable, "-m", "pip", "install", *mapped,
           "--no-index", "--target", str(tmp_path / "install-target"),
           "--platform", "linux_x86_64", "--python-version", "3.12",
           "--implementation", "cp", "--abi", "cp312",
           "--only-binary=:all:"]
    rc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert rc.returncode == 0, (
        f"the REAL install command (with the real wheel name) must work:\n"
        f"{' '.join(cmd)}\n{rc.stdout}\n{rc.stderr}")
    assert (tmp_path / "install-target" / "lmcache" / "__init__.py").is_file(), (
        "the installed package must be present")

    # -- the OLD fixed name must fail with pip's exact hardware error
    bad = tmpdir / "kvp-lmcache.whl"
    staged.rename(bad)
    rc2 = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
         "--quiet", "--target", str(tmp_path / "t2"), str(bad)],
        capture_output=True, text=True, timeout=120)
    assert rc2.returncode != 0, "kvp-lmcache.whl must be rejected by pip"
    out2 = rc2.stdout + rc2.stderr
    assert "Invalid wheel filename" in out2 and "wrong number of parts" in out2, (
        f"the hardware error must appear:\n{out2}")


# ---------------------------------------------------------------------------
# (c) mutation detectors: each mutation makes check_dockerfile report the
# violation -- the tests (a)/(b) are RED on the mutants
# ---------------------------------------------------------------------------
def _mutant_dockerfile_store(tmp_path: pathlib.Path, mutate) -> pathlib.Path:
    """Generate the Dockerfile through the REAL build-image.sh, then apply
    `mutate(text, wheel_basename) -> text` to the saved Dockerfile. Returns
    (store, wheel_basename)."""
    patch_dir, build_dir, out_dir, whl = make_real_wheel_fixture(tmp_path)
    r, store = _run_image(tmp_path, patch_dir, build_dir, out_dir)
    assert r.returncode == 0, r.stderr[-800:]
    df = store / "Dockerfile"
    df.write_text(mutate(df.read_text(), whl.name))
    return store, whl.name


def test_mutation_copy_dst_only(tmp_path):
    """Mutation: rename ONLY COPY dst -> check_dockerfile reports it (the
    old tests passed green on this)."""
    def mutate(text: str, whl_name: str) -> str:
        return text.replace(f"COPY {whl_name} /tmp/{whl_name}",
                            f"COPY {whl_name} /tmp/kvp-lmcache.whl")
    store, whl_name = _mutant_dockerfile_store(tmp_path, mutate)
    viol = check_dockerfile(store, whl_name)
    assert viol, "the COPY-dst-only rename must be reported"


def test_mutation_pip_path_only(tmp_path):
    """Mutation: rename ONLY the pip install path -> reported."""
    def mutate(text: str, whl_name: str) -> str:
        return text.replace(f'pip install --no-cache-dir --no-deps "/tmp/{whl_name}"',
                            'pip install --no-cache-dir --no-deps /tmp/kvp-lmcache.whl')
    store, whl_name = _mutant_dockerfile_store(tmp_path, mutate)
    viol = check_dockerfile(store, whl_name)
    assert any("pip install path" in v for v in viol), (
        f"the pip-path-only rename must be reported:\n{viol}")


def test_mutation_rm_path_only(tmp_path):
    """Mutation: rename ONLY the rm -f path -> reported."""
    def mutate(text: str, whl_name: str) -> str:
        return text.replace(f'rm -f "/tmp/{whl_name}"',
                            'rm -f /tmp/kvp-lmcache.whl')
    store, whl_name = _mutant_dockerfile_store(tmp_path, mutate)
    viol = check_dockerfile(store, whl_name)
    assert any("rm -f path" in v for v in viol), (
        f"the rm-path-only rename must be reported:\n{viol}")


def test_mutation_no_deps_dropped(tmp_path):
    """Mutation: drop --no-deps -> reported (pip would pull dependencies
    into the base image and could replace vLLM/torch packages)."""
    def mutate(text: str, whl_name: str) -> str:
        return text.replace("--no-deps ", "")
    store, whl_name = _mutant_dockerfile_store(tmp_path, mutate)
    viol = check_dockerfile(store, whl_name)
    assert any("--no-deps" in v for v in viol), (
        f"the missing --no-deps must be reported:\n{viol}")