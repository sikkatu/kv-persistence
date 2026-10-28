"""Value-based retention policy for persistent KV entries.

LMCache's built-in L2 eviction is plain LRU. Where a finer policy is needed,
this module ranks entries so that eviction removes *stale, rarely reused, cheap*
entries first and protects frequently reused long prefixes whose recomputation
is expensive.

Value signals (all measured, never guessed):
- recency: last access timestamp
- reuse frequency: hit count since insertion
- prefix length: token count of the cached prefix (benefit of a hit)
- storage cost: bytes actually consumed on disk

Value score = hits_weight * hits + len_weight * log1p(tokens) + recency_bonus,
divided by storage cost. Entries with identical policy inputs keep a stable
(deterministic) order by key, so tests are reproducible.

This module never deletes files itself: it only produces an ordered eviction
plan. Applying the plan is the job of the admin CLI / L2 adapter delete calls,
which keep shared blocks referenced by other entries intact.

LIMITATION (review): `shared_refs` is a logical hint, NOT a verified
cross-namespace physical-object refcount. When an entry has shared_refs > 1,
`effective_freed_bytes` conservatively reports 0 bytes freed (the object may
still be referenced by other keys we cannot see). Until LMCache exposes a
reliable key->object refcount aligned with its storage engine, the planner's
"freed" numbers are best-effort LOWER BOUNDS, not guarantees. Do not size
operations assuming a specific reclaim amount from shared entries.
"""

from __future__ import annotations

import dataclasses
import math
import time
from typing import Dict, List, Optional


@dataclasses.dataclass
class EntryStats:
    key: str
    tokens: int            # cached prefix length in tokens
    bytes_on_disk: int     # storage consumed
    hits: int              # reuse count since insertion
    last_access_ts: float  # unix timestamp of last hit/insert
    shared_refs: int = 1   # how many logical entries share the underlying blocks

    def value_score(self, now: Optional[float] = None, half_life_h: float = 24.0) -> float:
        """Higher = more valuable to keep.

        Value score combines measured benefit (hits + prefix length) with
        freshness decay and divides by *effective* storage cost. shared_refs
        REVERSES the sharing penalty: a widely-shared entry is relatively
        cheaper per live consumer and freeing it (via evicting one ref) does
        not free the bytes for anyone else -- so shared entries score HIGHER,
        reflecting that removing them removes an entry without reclaiming its
        bytes. Actual bytes freed by evicting this entry =
        bytes_on_disk if shared_refs == 1 else 0 (the object survives for
        other sharers).
        """
        now = now or time.time()
        age_h = max(0.0, (now - self.last_access_ts) / 3600.0)
        recency = math.exp(-age_h / half_life_h)          # 1.0 fresh -> 0 stale
        benefit = 3.0 * self.hits + 1.5 * math.log1p(self.tokens)
        cost = max(1, self.bytes_on_disk)
        # A shared object survives eviction of one ref -- freeing little real
        # space. Keep shared entries longer: multiply score by sqrt(shared_refs)
        # (was 1/sqrt(shared_refs) -- a bug flagged in review: the old formula
        # penalised shared entries while the comment claimed the opposite).
        sharing = max(1, self.shared_refs) ** 0.5
        # bytes actually reclaimed if this entry is evicted (last ref frees all)
        self.effective_freed_bytes = self.bytes_on_disk if self.shared_refs <= 1 else 0
        return (benefit * (0.3 + 0.7 * recency)) / cost * sharing * 1e9


def rank_for_eviction(entries: List[EntryStats], now: Optional[float] = None) -> List[EntryStats]:
    """Entries in eviction order: evict the LEAST valuable first. Stable tiebreak."""
    now = now or time.time()
    return sorted(entries, key=lambda e: (e.value_score(now), e.key))


def select_victims(
    entries: List[EntryStats],
    bytes_to_free: int,
    now: Optional[float] = None,
    min_keep_tokens: int = 0,
) -> List[EntryStats]:
    """Smallest set of least-valuable entries covering bytes_to_free.

    Entries with tokens >= min_keep_tokens AND hits > 0 are considered 'valuable
    long prefixes' and are only picked when cheaper candidates are exhausted.
    """
    now = now or time.time()
    normal: List[EntryStats] = []
    protected: List[EntryStats] = []
    for e in entries:
        if min_keep_tokens and e.tokens >= min_keep_tokens and e.hits > 0:
            protected.append(e)
        else:
            normal.append(e)
    victims: List[EntryStats] = []
    freed = 0
    for e in rank_for_eviction(normal, now) + rank_for_eviction(protected, now):
        if freed >= bytes_to_free:
            break
        victims.append(e)
        # Count EFFECTIVE bytes freed: a shared object (shared_refs > 1) only
        # frees its bytes when the LAST reference goes away. Until a reliable
        # cross-namespace refcount aligned with LMCache's actual key->object
        # mapping exists, effective_freed_bytes assumes the worst case for
        # shared entries (0 bytes freed) -- never overpromise reclaimed space.
        freed += e.bytes_on_disk if e.shared_refs <= 1 else 0
    return victims


def eviction_report(
    before: List[EntryStats], victims: List[EntryStats], now: Optional[float] = None
) -> Dict[str, object]:
    now = now or time.time()
    vkeys = {v.key for v in victims}
    kept_kept_long = [
        e.key for e in before
        if e.key not in vkeys and e.tokens > 0 and e.hits > 0
    ]
    return {
        "entries_before": len(before),
        "evicted_entries": len(victims),
        # Effective freed bytes (0 for shared entries) -- see module LIMITATION
        # note on refcount limits. Nominal sum kept for transparency.
        "evicted_bytes": sum(v.bytes_on_disk if v.shared_refs <= 1 else 0 for v in victims),
        "evicted_bytes_nominal": sum(v.bytes_on_disk for v in victims),
        "shared_victims_bytes_not_reclaimed": sum(
            v.bytes_on_disk for v in victims if v.shared_refs > 1
        ),
        "evicted_keys": [v.key for v in victims],   # keys only, no prompt text
        "kept_valuable_prefixes": kept_kept_long,
    }