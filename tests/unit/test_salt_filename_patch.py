"""Unit tests: the cache_salt L2 filename patch (disk-level isolation fix)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))


def _fake_key(model_name="mimo", kv_rank=1, object_group_id=0, chunk_hash=b"\xab" * 32,
              cache_salt=""):
    class _K:
        pass
    k = _K()
    k.model_name = model_name
    k.kv_rank = kv_rank
    k.object_group_id = object_group_id
    k.chunk_hash = chunk_hash
    k.cache_salt = cache_salt
    return k


def test_unsalted_name_matches_upstream_format():
    """No salt == exactly the upstream LMCache 0.5.3 file name (compat)."""
    from kvpersist.salt_filename_patch import patched_object_key_to_filename
    k = _fake_key(cache_salt="")
    name = patched_object_key_to_filename(k)
    assert name == f"mimo_{1:08x}_0_{'ab' * 32}.bin", name


def test_salted_name_appends_salt():
    """The salt MUST be in the file name -- this is the disk-level isolation."""
    from kvpersist.salt_filename_patch import patched_object_key_to_filename
    a = patched_object_key_to_filename(_fake_key(cache_salt="tenantA"))
    b = patched_object_key_to_filename(_fake_key(cache_salt="tenantB"))
    assert a.endswith("_tenantA.bin"), a
    assert b.endswith("_tenantB.bin"), b
    assert a != b, "different salts must map to different L2 file names"


def test_same_salt_same_name():
    from kvpersist.salt_filename_patch import patched_object_key_to_filename
    a = patched_object_key_to_filename(_fake_key(cache_salt="tenantA"))
    b = patched_object_key_to_filename(_fake_key(cache_salt="tenantA"))
    assert a == b


def test_salt_with_slash_is_sanitized():
    """ObjectKey forbids '/' in the salt, but sanitize defensively anyway."""
    from kvpersist.salt_filename_patch import patched_object_key_to_filename
    n = patched_object_key_to_filename(_fake_key(cache_salt="ten/ant"))
    assert "/" not in n, n
    assert n.endswith("_ten--ant.bin"), n