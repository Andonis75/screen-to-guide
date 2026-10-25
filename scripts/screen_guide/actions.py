"""Validate traceable action evidence before publishing a reading guide.

This checks consistency of the declared observations. It does not infer clicks
from pixels or establish that an Agent's semantic interpretation is correct.
"""
from __future__ import annotations

import math

KINDS = {"click", "input", "select", "navigate", "automatic", "wait"}
SUPPORTS = {"observed_transition", "visible_value", "system_transition", "inferred"}
DIRECT_ACTIONS = {"click", "navigate"}
_EPSILON = 1e-6


def _nonempty_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _seconds(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0 or value > (2**53 // 10) or not math.isfinite(value)):
        raise ValueError(f"{name} must be finite nonnegative seconds")
    return value


def _references(value, name):
    if (not isinstance(value, list) or not value or len(value) > 5000
            or not all(isinstance(item, str) and item for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{name} must be a nonempty array of unique ids")
    return value


def validate_action_evidence(data):
    """Validate a renderer's already-copied, frame-validated manifest in place.

    Legacy guides remain compatible when the flag and evidence are absent.
    In strict mode every instruction must cite a supported evidence record.
    """
    tutorial = data.get("tutorial", {})
    required = tutorial.get("action_evidence_required", False)
    if not isinstance(required, bool):
        raise ValueError("tutorial.action_evidence_required must be boolean")
    if not required and "action_evidence" not in data:
        return
    records = data.get("action_evidence", [])
    if not isinstance(records, list) or len(records) > 5000:
        raise ValueError("action_evidence must be a bounded array")
    duration = data.get("source", {}).get("duration_seconds")
    if required and duration is None:
        raise ValueError("Strict action evidence requires source.duration_seconds")
    frames = {frame["id"]: frame for frame in data["frames"]}
    evidence = {}
    event_kinds = {}
    footage_kinds = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("action_evidence entries must be objects")
        record_id = _nonempty_text(record.get("id"), "action evidence id")
        if record_id in evidence:
            raise ValueError("action evidence ids must be unique")
        kind = record.get("kind")
        support = record.get("support")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError(f"Unknown action evidence kind: {record_id}")
        if not isinstance(support, str) or support not in SUPPORTS:
            raise ValueError(f"Unknown action evidence support: {record_id}")
        _nonempty_text(record.get("description"), "action evidence description")
        event_id = _nonempty_text(record.get("event_id", record_id), "action evidence event_id")
        start = _seconds(record.get("start_seconds"), "action evidence start_seconds")
        end = _seconds(record.get("end_seconds"), "action evidence end_seconds")
        if start > end or (duration is not None and end > duration):
            raise ValueError(f"Action evidence interval is outside source: {record_id}")
        refs = _references(record.get("frame_ids"), "action evidence frame_ids")
        if any(frame_id not in frames for frame_id in refs):
            raise ValueError(f"Action evidence references an unknown frame: {record_id}")
        times = [frames[frame_id]["timestamp_seconds"] for frame_id in refs]
        if any(t < start - _EPSILON or t > end + _EPSILON for t in times):
            raise ValueError(f"Action evidence frame is outside its interval: {record_id}")
        if required:
            allowed = {
                "click": {"observed_transition"},
                "navigate": {"observed_transition"},
                "input": {"observed_transition", "visible_value"},
                "select": {"observed_transition", "visible_value"},
                "automatic": {"system_transition"},
                "wait": {"system_transition", "observed_transition"},
            }
            if support not in allowed[kind]:
                raise ValueError(f"Action evidence support does not justify {kind}: {record_id}")
            if kind in DIRECT_ACTIONS and max(times) - min(times) <= _EPSILON:
                raise ValueError(f"{kind} evidence needs before/after frames: {record_id}")
        # Both keys matter: a renamed event must not turn the same observed
        # automatic transition into a second, supposedly manual click.
        event_kinds.setdefault(event_id, set()).add(kind)
        footage_key = (tuple(sorted(times)), start, end)
        footage_kinds.setdefault(footage_key, set()).add(kind)
        evidence[record_id] = record
    if required:
        for kinds in (*event_kinds.values(), *footage_kinds.values()):
            if "automatic" in kinds and kinds & DIRECT_ACTIONS:
                raise ValueError("Conflicting automatic and manual action evidence for one event")
        if not data["steps"]:
            raise ValueError("Strict reading guide needs at least one step")
    for step in data["steps"]:
        if not required and "action_refs" not in step:
            continue
        refs = step.get("action_refs")
        actions = step["actions"]
        if required and not actions:
            raise ValueError(f"Strict reading guide needs actions: {step['id']}")
        if not isinstance(refs, list) or len(refs) != len(actions):
            raise ValueError(f"action_refs must align one-to-one with actions: {step['id']}")
        for action_refs in refs:
            _references(action_refs, "action_refs entry")
            if any(record_id not in evidence for record_id in action_refs):
                raise ValueError(f"Action references unknown evidence: {step['id']}")
