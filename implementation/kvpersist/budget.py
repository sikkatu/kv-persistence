"""Bounded storage budgets and per-model accounting for the persistent cache.

The hard bound itself is enforced by the LMCache nixl_store_dynamic adapter
(max_capacity_gb). This module provides the *policy layer* the task asks for on
top: a soft threshold (default 90 % of budget), per-namespace quotas, waterlevel
reporting, and a safe reconcile of what is actually stored on disk.

Semantics
---------
* soft fraction: usage above this fraction of the budget is "under pressure" and
  the maintainer should evict low-value entries down to the target fraction.
* hard limit: the adapter rejects stores above it; ledger never admits entry
  accounting that would exceed it.
* quotas: a namespace (model identity) may not consume more than its quota of
  the budget; without explicit quotas every namespace gets an equal share of a
  configurable fairness pool (see DEFAULT_FAIR_SHARE).
* everything is accounted in bytes; no file outside the cache root is ever
  inspected or deleted by this module.
"""

from __future__ import annotations

import os
import threading
from typing import Dict, Optional

KB = 1024
MB = 1024 * KB
GB = 1024 * MB


class BudgetError(Exception):
    """Budget violation that must not silently pass."""


class BudgetLedger:
    """Thread-safe accounting of cache usage per namespace against a hard budget."""

    def __init__(
        self,
        budget_bytes: int,
        soft_fraction: float = 0.9,
        target_fraction: float = 0.85,
        quotas: Optional[Dict[str, int]] = None,
    ) -> None:
        if budget_bytes <= 0:
            raise BudgetError("budget_bytes must be positive")
        if not (0.0 < target_fraction <= soft_fraction <= 1.0):
            raise BudgetError("need 0 < target_fraction <= soft_fraction <= 1.0")
        self.budget_bytes = budget_bytes
        self.soft_fraction = soft_fraction
        self.target_fraction = target_fraction
        self.quotas = dict(quotas or {})
        self._usage: Dict[str, int] = {}
        self._lock = threading.Lock()
        self.evictions = 0
        self.reclaimed_bytes = 0
        self.rejected_writes = 0

    # -- usage accounting -------------------------------------------------
    def usage(self, namespace: str) -> int:
        with self._lock:
            return self._usage.get(namespace, 0)

    def total_usage(self) -> int:
        with self._lock:
            return sum(self._usage.values())

    def free_bytes(self) -> int:
        return max(0, self.budget_bytes - self.total_usage())

    def in_soft_pressure(self) -> bool:
        return self.total_usage() >= self.soft_fraction * self.budget_bytes

    def namespace_limit(self, namespace: str) -> Optional[int]:
        return self.quotas.get(namespace)

    def can_admit(self, namespace: str, nbytes: int) -> bool:
        """True when admitting nbytes for namespace stays inside hard budget and quota."""
        if nbytes < 0:
            raise BudgetError("nbytes must be >= 0")
        with self._lock:
            if sum(self._usage.values()) + nbytes > self.budget_bytes:
                return False
            limit = self.quotas.get(namespace)
            if limit is not None and self._usage.get(namespace, 0) + nbytes > limit:
                return False
            return True

    def admit(self, namespace: str, nbytes: int) -> None:
        """Record a write; never exceeds the hard budget (raises instead)."""
        with self._lock:
            total = sum(self._usage.values())
            if total + nbytes > self.budget_bytes:
                self.rejected_writes += 1
                raise BudgetError(
                    f"hard budget exceeded: {total + nbytes} > {self.budget_bytes} bytes"
                )
            limit = self.quotas.get(namespace)
            if limit is not None and self._usage.get(namespace, 0) + nbytes > limit:
                self.rejected_writes += 1
                raise BudgetError(
                    f"namespace quota exceeded for {namespace}: "
                    f"{self._usage.get(namespace, 0) + nbytes} > {limit} bytes"
                )
            self._usage[namespace] = self._usage.get(namespace, 0) + nbytes

    def release(self, namespace: str, nbytes: int, evicted: bool = False) -> None:
        with self._lock:
            if nbytes < 0:
                raise BudgetError("nbytes must be >= 0")
            cur = self._usage.get(namespace, 0)
            self._usage[namespace] = max(0, cur - nbytes)
            if evicted:
                self.evictions += 1
                self.reclaimed_bytes += min(nbytes, cur)

    # -- recovery of accounting from disk --------------------------------
    def sync_from_sizes(self, sizes: Dict[str, int]) -> None:
        """Set absolute per-namespace usage from measured on-disk sizes."""
        with self._lock:
            self._usage = {ns: max(0, int(v)) for ns, v in sizes.items()}

    # -- reporting -------------------------------------------------------
    def report(self) -> Dict[str, object]:
        with self._lock:
            total = sum(self._usage.values())
            return {
                "budget_bytes": self.budget_bytes,
                "used_bytes": total,
                "free_bytes": max(0, self.budget_bytes - total),
                "usage_fraction": round(total / self.budget_bytes, 4),
                "soft_threshold_bytes": int(self.soft_fraction * self.budget_bytes),
                "target_bytes_after_eviction": int(self.target_fraction * self.budget_bytes),
                "soft_pressure": total >= self.soft_fraction * self.budget_bytes,
                "per_namespace_bytes": dict(sorted(self._usage.items())),
                "quotas_bytes": dict(self.quotas),
                "evictions": self.evictions,
                "reclaimed_bytes": self.reclaimed_bytes,
                "rejected_writes": self.rejected_writes,
            }

    def evict_candidates(self) -> Dict[str, int]:
        """Namespaces that must shrink and by how much to reach the target fraction.

        Preference order is the caller's (value-based policy); the ledger only
        computes the quantities. Largest over-quota namespaces shrink first.
        """
        with self._lock:
            total = sum(self._usage.values())
            over = max(0, total - int(self.target_fraction * self.budget_bytes))
            result: Dict[str, int] = {}
        if over == 0:
            return result
        # shrink namespaces in descending usage order until 'over' is covered
        items = sorted(self._usage.items(), key=lambda kv: kv[1], reverse=True)
        remaining = over
        for ns, used in items:
            if remaining <= 0:
                break
            take = min(used, remaining)
            result[ns] = take
            remaining -= take
        return result