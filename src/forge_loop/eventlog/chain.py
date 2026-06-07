"""Hash-chained integrity for the durable event log (issue #338).

Each appended event stores ``event_hash = H(prev_hash, immutable fields)`` where
``prev_hash`` is the ``event_hash`` of the previous event in log order. On the
read path (:meth:`SqliteEventLog.since`) the chain is recomputed from the
canonical payload + running prev-hash and compared to the stored value; the
first sequence whose hash or chain link no longer recomputes raises
:class:`EventChainIntegrityError` instead of yielding poisoned events. This makes
replay fail loudly on a tampered, truncated, or reordered log rather than fold
corrupted state into projections.
"""

from __future__ import annotations

import hashlib
import json

#: Synthetic predecessor hash for the first event in the chain. A 64-char zero
#: string mirrors the sha256 hex width so the genesis link is unmistakable.
GENESIS_HASH = "0" * 64


class EventChainIntegrityError(RuntimeError):
    """Raised when a stored event hash or chain link fails to recompute.

    Carries the :attr:`sequence` of the *first* event whose hash/chain no longer
    matches its stored value so the operator (and boot reconstruction) learns
    exactly where the log stopped being trustworthy.
    """

    def __init__(self, sequence: int, detail: str) -> None:
        self.sequence = sequence
        super().__init__(f"event log integrity broken at sequence {sequence}: {detail}")


def compute_event_hash(
    *,
    prev_hash: str,
    event_id: str,
    kind: str,
    payload_json: str,
    schema_version: int,
    occurred_at: str,
    task_id: str | None,
    saga_id: str | None,
    idempotency_key: str | None,
) -> str:
    """Return the deterministic chain hash for one event.

    ``payload_json`` must already be the canonical (sorted-key, compact) JSON
    that was persisted. The remaining immutable envelope fields are folded in via
    canonical JSON so ``None`` is distinguishable from the empty string.
    """

    material = json.dumps(
        [
            prev_hash,
            event_id,
            kind,
            payload_json,
            schema_version,
            occurred_at,
            task_id,
            saga_id,
            idempotency_key,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
