"""Derive QualityEvidence from recorded task results, read-only (#80).

PR #82's shadow report over live post-#334 databases produced 447 artifacts
that were all ``risk_tier=safety_or_live`` with ``insufficient_evidence``
treatments. That was the fail-safe working as designed -- no caller supplied
``QualityEvidence``, so every risk fact was unknown -- but it also meant the
report carried no signal. This module closes the gap the only honest way:
by reading evidence the producer actually recorded, and saying plainly which
fields are not recorded at all rather than inventing them.

⛔What is derived here is derived from STRUCTURED, RECORDED fields only. No
  field is inferred from a task's description, summary, prompt or any other
  free text: quota-core#80's non-goals forbid it, and a risk tier inferred
  from prose would be indistinguishable from a measured one downstream.

Every derivation carries provenance, so a reader of the report can tell
"measured false" from "never recorded" without reading this source.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path

from .policy import QualityEvidence

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Verdict strings the producer records, mapped to review correctness.
#: Anything else -- including NULL -- stays unknown rather than being read as
#: a rejection, because "no verdict yet" and "reviewer said no" are different
#: facts and only one of them is evidence about the work.
_VERDICT_TO_REVIEW_CORRECT: dict[str, bool] = {
    "approve": True,
    "approved": True,
    "request_changes": False,
    "changes_requested": False,
    "reject": False,
    "rejected": False,
}

#: Why a field is unknown, when it is.
NOT_RECORDED = "not_recorded_by_producer"
NO_LINKED_REVIEW = "no_linked_review_task"
NO_VERDICT_YET = "linked_review_has_no_verdict"

#: Task types whose recorded verdict counts as an INDEPENDENT REVIEW verdict.
#: Deliberately narrow, and deliberately checked rather than assumed: review of
#: PR #84 found that accepting any row carrying a verdict plus a linked task
#: silently let other work speak as a reviewer. On real local data 12 rows of
#: type `test` carry both, and their verdicts were being attributed as review
#: correctness for the tasks they pointed at.
#:
#: ⛔A tester's verdict is its own kind of evidence, not this one. A caller
#:   that wants it counted must pass it in explicitly, so the substitution is
#:   a visible decision rather than an accident of table shape.
REVIEW_TASK_TYPES: frozenset[str] = frozenset({"review", "reviewer"})

#: Why no verdict was read from a row that had one.
NOT_A_REVIEW_ROW = "verdict_row_is_not_a_review_task"


@dataclass(frozen=True)
class TaskEvidence:
    """One task's derived evidence plus why each field says what it says."""

    task_id: str
    evidence: QualityEvidence
    provenance: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {"task_id": self.task_id, "provenance": dict(self.provenance)}


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    path = Path(db_path).expanduser()
    if not path.is_file():
        return None
    try:
        # Read-only: the source can be a live runtime database and this module
        # must never be able to modify one.
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _linked_target(context: object) -> str | None:
    """The task a review row reviewed, from its recorded context payload."""
    if isinstance(context, (str, bytes)):
        try:
            context = json.loads(context or "{}")
        except (TypeError, ValueError):
            return None
    if not isinstance(context, dict):
        return None
    target = context.get("prev_task_id")
    return target if isinstance(target, str) and target else None


def read_review_verdicts(
    db_path: str | Path,
    table: str = "tasks",
    review_task_types: frozenset[str] = REVIEW_TASK_TYPES,
) -> dict[str, tuple[str, str]]:
    """Return ``{reviewed_task_id: (verdict, reviewing_task_id)}``, read-only.

    Only rows whose recorded ``task_type`` is in ``review_task_types`` are
    read. A verdict on any other kind of row is ignored: carrying a verdict and
    a linked task does not make a row a review, and letting one speak as a
    reviewer would fabricate the single piece of quality evidence this module
    derives.

    ⛔A table with no ``task_type`` column yields ``{}``. Without it there is no
      way to confirm a row is a review, and "cannot confirm" must not become
      "assume yes" for evidence that gates quality downstream.

    A review row links to the work it reviewed through its recorded context
    payload, not through the attribution session chain (that chain links a
    review to the PREVIOUS review, which is a different relationship). When a
    task was reviewed more than once -- a fix loop -- the LATEST review wins,
    ordered by creation time with the task id breaking ties so the result is
    deterministic. An unreadable or incompatible database yields ``{}``.
    """
    conn = _connect_readonly(db_path)
    if conn is None or not _SAFE_IDENTIFIER.fullmatch(table):
        if conn is not None:
            conn.close()
        return {}
    try:
        conn.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            # `task_type` is required, not optional: see the docstring.
            if not {"task_id", "task_type", "context", "verdict"} <= columns:
                return {}
            order = "created_at, task_id" if "created_at" in columns else "task_id"
            rows = conn.execute(
                f'SELECT task_id, task_type, context, verdict FROM "{table}" ORDER BY {order}'
            ).fetchall()
        except sqlite3.Error:
            return {}
    finally:
        conn.close()

    latest: dict[str, tuple[str, str]] = {}
    allowed = {kind.strip().lower() for kind in review_task_types}
    for row in rows:
        task_type = row["task_type"]
        if not isinstance(task_type, str) or task_type.strip().lower() not in allowed:
            continue
        target = _linked_target(row["context"])
        verdict = row["verdict"]
        # A task cannot independently review itself. Treat a self-link as no
        # evidence instead of allowing a task's own verdict to pass its gate.
        if (not target or target == row["task_id"]
                or not isinstance(verdict, str) or not verdict.strip()):
            continue
        # Ascending order means a later row legitimately replaces an earlier
        # one: the last verdict on a fix loop is the standing one.
        latest[target] = (verdict.strip().lower(), str(row["task_id"]))
    return latest


def derive_quality_evidence(
    db_path: str | Path,
    task_ids: list[str] | None = None,
    table: str = "tasks",
    review_task_types: frozenset[str] = REVIEW_TASK_TYPES,
) -> dict[str, TaskEvidence]:
    """Derive per-task :class:`QualityEvidence` from recorded results.

    Derived today:

    - ``independent_review_correct`` -- from a linked REVIEW task's recorded
      ``verdict``. ``approve`` is True, ``request_changes``/``reject`` is
      False, and no linked review or no verdict yet stays ``None``. A verdict
      recorded on a non-review row is ignored (see
      :func:`read_review_verdicts`).

    Deliberately NOT derived, and reported as such rather than guessed:

    - ``required_context_recalled`` -- no producer field records whether the
      context a task needed was actually present. Because the policy's quality
      gate requires this to be True, every task read from a database alone
      stays below the gate; that is an honest ``insufficient_evidence``, and
      the provenance says which field caused it. This is the single missing
      producer signal that would let measured quality conclusions exist.
    - the four risk facts -- nothing records whether a task touched live or
      safety-relevant surface, so they stay ``None`` and the policy's
      fail-safe assigns the highest-scrutiny tier plus a human gate. Inferring
      them from a description would be exactly the fabrication #80 forbids.

    A caller that HAS these facts from somewhere else should pass its own
    ``QualityEvidence`` instead of, or merged over, this result.
    """
    verdicts = read_review_verdicts(db_path, table, review_task_types)
    wanted = list(task_ids) if task_ids is not None else sorted(verdicts)
    derived: dict[str, TaskEvidence] = {}
    for task_id in wanted:
        review_correct: bool | None = None
        if task_id in verdicts:
            verdict, reviewer = verdicts[task_id]
            review_correct = _VERDICT_TO_REVIEW_CORRECT.get(verdict)
            review_source = (
                f"review_verdict:{verdict}:{reviewer}"
                if review_correct is not None else NO_VERDICT_YET
            )
        else:
            review_source = NO_LINKED_REVIEW
        derived[task_id] = TaskEvidence(
            task_id=task_id,
            evidence=QualityEvidence(independent_review_correct=review_correct),
            provenance={
                "independent_review_correct": review_source,
                "required_context_recalled": NOT_RECORDED,
                "safety_or_live_change": NOT_RECORDED,
                "broad_architecture_change": NOT_RECORDED,
                "bounded_routine_fix": NOT_RECORDED,
                "human_gate_required": NOT_RECORDED,
            },
        )
    return derived


def evidence_map(derived: dict[str, TaskEvidence]) -> dict[str, QualityEvidence]:
    """Reduce to the ``{task_id: QualityEvidence}`` the policy engine takes."""
    return {task_id: item.evidence for task_id, item in derived.items()}


#: Provenance marker for a fact an operator asserted, rather than one the
#: producer measured. Kept distinct on purpose: a reader must always be able
#: to tell a declared policy from an observation.
OPERATOR_DECLARED = "operator_declared_by_task_type"


@dataclass(frozen=True)
class TaskTypeRiskDeclaration:
    """An operator's explicit declaration of what a task TYPE implies (#80).

    ⛔OPT-IN, and never a default. Nothing in a task-attribution database
      records whether a task touched live or safety-relevant surface, so the
      honest default stays unknown and the policy's fail-safe assigns the
      highest-scrutiny tier plus a human gate. That is correct but carries no
      signal, which is what PR #82's 447 uniformly-``safety_or_live``
      artifacts showed.

      A deployment that genuinely knows "in my fleet, a task of type `review`
      does not change live surface" can say so here. That is a DECLARATION,
      not a measurement, and it is recorded as such in provenance so no
      downstream reader can mistake one for the other.

    ⛔Only ever declare a task type DOWN to lower scrutiny when the operator
      is sure. An unlisted type stays unknown and keeps the fail-safe. If a
      type appears in more than one tier category, safety_or_live takes
      precedence, then architecture, routine, and non-production. The human
      gate is orthogonal to those tiers and is retained with any of them. A
      broader declaration must never dilute an explicit safety or gate
      declaration.
    """

    safety_or_live_types: frozenset[str] = frozenset()
    architecture_types: frozenset[str] = frozenset()
    routine_types: frozenset[str] = frozenset()
    human_gate_types: frozenset[str] = frozenset()
    #: Types the operator declares are none of the above -- e.g. read-only
    #: review or test work that produces a verdict rather than a change.
    non_production_types: frozenset[str] = frozenset()

    def declare(self, task_type: str | None) -> QualityEvidence | None:
        """Risk facts for this task type, or ``None`` to leave it unknown."""
        if not task_type:
            return None
        kind = task_type.strip().lower()
        known = (
            self.safety_or_live_types | self.architecture_types
            | self.routine_types | self.human_gate_types | self.non_production_types
        )
        if kind not in known:
            return None
        gate = kind in self.human_gate_types
        if kind in self.safety_or_live_types:
            return QualityEvidence(
                safety_or_live_change=True,
                broad_architecture_change=False,
                bounded_routine_fix=False,
                human_gate_required=gate,
            )
        if kind in self.architecture_types:
            return QualityEvidence(
                safety_or_live_change=False,
                broad_architecture_change=True,
                bounded_routine_fix=False,
                human_gate_required=gate,
            )
        if kind in self.routine_types:
            return QualityEvidence(
                safety_or_live_change=False,
                broad_architecture_change=False,
                bounded_routine_fix=True,
                human_gate_required=gate,
            )
        if kind in self.human_gate_types:
            return QualityEvidence(
                safety_or_live_change=False,
                broad_architecture_change=False,
                bounded_routine_fix=False,
                human_gate_required=gate,
            )
        return QualityEvidence(
            safety_or_live_change=False,
            broad_architecture_change=False,
            bounded_routine_fix=False,
            human_gate_required=gate,
        )


def apply_risk_declaration(
    derived: dict[str, TaskEvidence],
    task_types: dict[str, str | None],
    declaration: TaskTypeRiskDeclaration,
) -> dict[str, TaskEvidence]:
    """Overlay an operator's declared risk facts onto derived evidence.

    Measured fields (the review verdict) are preserved untouched; only the
    four risk facts are filled, and only for task types the operator listed.
    Provenance records every filled field as operator-declared.
    """
    updated: dict[str, TaskEvidence] = {}
    for task_id, item in derived.items():
        declared = declaration.declare(task_types.get(task_id))
        if declared is None:
            updated[task_id] = item
            continue
        provenance = dict(item.provenance)
        for name in (
            "safety_or_live_change", "broad_architecture_change",
            "bounded_routine_fix", "human_gate_required",
        ):
            provenance[name] = f"{OPERATOR_DECLARED}:{task_types.get(task_id)}"
        updated[task_id] = TaskEvidence(
            task_id=task_id,
            evidence=replace(
                item.evidence,
                safety_or_live_change=declared.safety_or_live_change,
                broad_architecture_change=declared.broad_architecture_change,
                bounded_routine_fix=declared.bounded_routine_fix,
                human_gate_required=declared.human_gate_required,
            ),
            provenance=provenance,
        )
    return updated
