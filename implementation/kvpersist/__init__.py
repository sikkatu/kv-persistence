"""kvpersist: model-agnostic cache identity, budget accounting and admin CLI.

Storage, persistence and eviction are provided by LMCache's nixl_store_dynamic
L2 adapter. This package covers what the task requires to be explicit and
testable:

* cache identity / compatibility namespaces (identity.py)
* bounded storage budgets and per-model quotas (budget.py)
* administrative status / purge / limit reporting (cli.py)

Stdlib-only on purpose: it must run on the host, in tests and in minimal
environments without pulling LMCache or torch.
"""

from .identity import CacheIdentity, IdentityError
from .budget import BudgetLedger, BudgetError

__all__ = ["CacheIdentity", "IdentityError", "BudgetLedger", "BudgetError"]