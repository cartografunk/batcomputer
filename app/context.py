"""Deterministic context shared by the code agents."""

import hashlib


def uses_candidate(state: dict) -> bool:
    return (state.get("route", {}).get("kind") != "QUESTION" and
            state.get("route", {}).get("base") == "candidate" and
            bool(state.get("candidate_files")))


def base_files(state: dict) -> list[dict]:
    return state.get("candidate_files", []) if uses_candidate(state) else state.get("previous_files", [])


def prior_criteria(state: dict) -> list[dict]:
    return state.get("candidate_criteria", []) if uses_candidate(state) else state.get("approved_criteria", [])


def accumulated_criteria(state: dict) -> list[dict]:
    criteria = list(prior_criteria(state))
    for item in (state.get("plan") or {}).get("criteria", []):
        if item not in criteria:
            criteria.append(item)
    return criteria


def changes_vs_base(base: list[dict], proposed: list[dict]) -> dict:
    old = {item["path"]: hashlib.sha256(item["content"].encode()).hexdigest()
           for item in base}
    new = {item["path"]: hashlib.sha256(item["content"].encode()).hexdigest()
           for item in proposed}
    return {
        "added": [{"path": path, "sha256": new[path]} for path in sorted(new.keys() - old.keys())],
        "modified": [{"path": path, "before_sha256": old[path], "after_sha256": new[path]}
                     for path in sorted(old.keys() & new.keys()) if old[path] != new[path]],
        "deleted": [{"path": path, "sha256": old[path]} for path in sorted(old.keys() - new.keys())],
    }
