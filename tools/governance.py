"""Governance layer: decision traceability + human-in-the-loop gate.

Every agent decision — extraction, retrieval, each reconciliation rule,
each compliance check, each answer — is appended to an audit trail
(trace.jsonl). Nothing is ever "filed" by the agent: the audit report ends
in PENDING_HUMAN_REVIEW and only an explicit human approval (recorded, with
reviewer name, in the same trail) moves it to APPROVED.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class TraceLog:
    """Append-only JSONL audit trail of every agent decision."""

    def __init__(self, path: str | Path = "trace.jsonl", run_id: str | None = None):
        self.path = Path(path)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._n = 0

    def log(self, action: str, **payload) -> dict:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "run_id": self.run_id,
            "seq": self._n,
            "action": action,
            **payload,
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        self._n += 1
        return entry

    @property
    def entries_written(self) -> int:
        return self._n


class ReviewGate:
    """Human-in-the-loop validation gate.

    The pipeline can only ever produce a report in PENDING_HUMAN_REVIEW
    status. `approve()` is the single path to APPROVED and it requires a
    named human reviewer — there is no auto-approve, and the approval itself
    is written to the audit trail.
    """

    PENDING = "PENDING_HUMAN_REVIEW"
    APPROVED = "APPROVED_BY_HUMAN"

    def __init__(self, trace: TraceLog):
        self.trace = trace
        self.status = self.PENDING

    def approve(self, reviewer: str, note: str = "") -> str:
        reviewer = (reviewer or "").strip()
        if not reviewer:
            raise ValueError("A named human reviewer is required to approve.")
        self.status = self.APPROVED
        self.trace.log("governance.human_approval", reviewer=reviewer, note=note)
        return self.status
