from datetime import timedelta

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import select

from app.db import Conversation, Run, Snapshot, Trace, now
from app.service import claim_next, followup_context, owner_hash, process_run, turn_ledger
from app.snapshots import snapshot_files
from test_flow import CODE, NO, PLAN, YES, FakeProvider, config, execute, queued, store


REJECTED = {"summary": "Rejected", "files": [
    {"path": "app.py", "content": "print('rejected')"}]}
FIXED = {"summary": "Fixed", "files": [
    {"path": "app.py", "content": "print('fixed')"}]}
PLAN_V2 = {"kind": "code", "summary": "Change app", "criteria": [
    {"description": "Keep output valid", "verification": "Inspect app.py"}]}
PLAN_V3 = {"kind": "code", "summary": "Fix reviewer issues", "criteria": [
    {"description": "Fix output", "verification": "Inspect app.py"}]}


class CandidateRoutingProvider(FakeProvider):
    def call(self, role, payload):
        if role == "router" and payload.get("has_candidate") and payload.get("has_head"):
            self.calls.append((role, payload))
            return {"kind": "MODIFICATION", "base": "candidate", "reason": "Fix rejected candidate"}
        return super().call(role, payload)


class NewTicketRoutingProvider(FakeProvider):
    def call(self, role, payload):
        if role == "router":
            self.calls.append((role, payload))
            return {"kind": "NEW_TICKET", "base": "candidate", "reason": "Another project"}
        return super().call(role, payload)


class _Crash(BaseException):
    pass


class CrashAfterCandidateRoute(CandidateRoutingProvider):
    def call(self, role, payload):
        if role == "planner":
            raise _Crash()
        return super().call(role, payload)


def _approved_then_exhausted(store, config):
    conversation, first_id = queued(store)
    execute(store, config, first_id, FakeProvider([PLAN, CODE, YES]))
    _, second_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                          content="Change app")
    execute(store, config, second_id, FakeProvider([
        PLAN_V2, REJECTED, NO, REJECTED, NO, REJECTED, NO]))
    with store() as db:
        row = db.get(Conversation, conversation)
        return conversation, row.head_snapshot_id, row.candidate_snapshot_id


def test_explicit_correction_uses_rejected_base_and_feedback_for_all_agents(store, config):
    conversation, head_id, candidate_id = _approved_then_exhausted(store, config)
    _, run_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                       content="Corrige el candidato rechazado")
    fake = CandidateRoutingProvider([PLAN_V3, FIXED, YES])
    run = execute(store, config, run_id, fake)
    assert run.status == "approved"
    router = next(payload for role, payload in fake.calls if role == "router")
    planner = next(payload for role, payload in fake.calls if role == "planner")
    coder = next(payload for role, payload in fake.calls if role == "coder")
    reviewer = next(payload for role, payload in fake.calls if role == "reviewer")
    assert router["has_head"] and router["has_candidate"]
    assert router["candidate_feedback_summary"] == "Needs change"
    assert planner["files"] == REJECTED["files"]
    assert planner["candidate_feedback"] == ["Fix output"]
    assert planner["prior_criteria"] == PLAN["criteria"] + PLAN_V2["criteria"]
    assert coder["previous_files"] == REJECTED["files"]
    assert coder["review_feedback"] == ["Fix output"]
    assert reviewer["changes_vs_base"]["modified"][0]["path"] == "app.py"
    assert reviewer["changes_vs_base"]["deleted"] == []
    assert PLAN_V3["criteria"][0] in reviewer["all_criteria"]
    with store() as db:
        updated = db.get(Run, run_id)
        snapshot = db.get(Snapshot, updated.result_snapshot_id)
        route_trace = db.scalar(select(Trace).where(Trace.run_id == run_id, Trace.stage == "router"))
        assert route_trace.output["base"] == "candidate"
        assert updated.base_snapshot_id == candidate_id
        assert snapshot.parent_snapshot_id == candidate_id
        row = db.get(Conversation, conversation)
        assert row.head_snapshot_id == snapshot.id
        assert row.candidate_snapshot_id is None
        assert head_id != candidate_id


def test_new_request_after_rejection_uses_approved_base(store, config):
    conversation, head_id, candidate_id = _approved_then_exhausted(store, config)
    _, run_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                       content="Agrega logout")
    fake = FakeProvider([PLAN, CODE, YES])
    assert execute(store, config, run_id, fake).status == "approved"
    planner = next(payload for role, payload in fake.calls if role == "planner")
    coder = next(payload for role, payload in fake.calls if role == "coder")
    reviewer = next(payload for role, payload in fake.calls if role == "reviewer")
    assert planner["files"] == CODE["files"]
    assert coder["previous_files"] == CODE["files"]
    assert coder["review_feedback"] == []
    assert reviewer["changes_vs_base"] == {"added": [], "modified": [], "deleted": []}
    with store() as db:
        run = db.get(Run, run_id)
        assert run.base_snapshot_id == head_id
        assert db.get(Snapshot, run.result_snapshot_id).parent_snapshot_id == head_id
        assert db.get(Conversation, conversation).candidate_snapshot_id is None
        assert candidate_id != head_id


def test_new_ticket_without_approved_head_ignores_rejected_candidate(store, config):
    conversation, rejected_id = queued(store)
    assert execute(store, config, rejected_id, FakeProvider([
        PLAN, REJECTED, NO, REJECTED, NO, REJECTED, NO])).status == "exhausted"
    _, run_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                       content="Una calculadora de propinas, otro proyecto")
    fake = NewTicketRoutingProvider([PLAN, CODE, YES])
    assert execute(store, config, run_id, fake).status == "approved"
    planner = next(payload for role, payload in fake.calls if role == "planner")
    coder = next(payload for role, payload in fake.calls if role == "coder")
    assert planner["files"] == []
    assert planner["prior_criteria"] == []
    assert planner["candidate_feedback"] == []
    assert coder["previous_files"] == []
    assert coder["review_feedback"] == []
    with store() as db:
        run = db.get(Run, run_id)
        snapshot = db.get(Snapshot, run.result_snapshot_id)
        route_trace = db.scalar(select(Trace).where(Trace.run_id == run_id, Trace.stage == "router"))
        assert route_trace.output["kind"] == "NEW_TICKET"
        assert route_trace.output["base"] == "approved"
        assert run.base_snapshot_id is None
        assert snapshot.parent_snapshot_id is None


def test_recovered_candidate_route_keeps_base_snapshot(store, config):
    baseline_conversation, _, _ = _approved_then_exhausted(store, config)
    _, baseline_id = queued(
        store, conversation=Conversation(id=baseline_conversation, owner_hash=owner_hash("alice")),
        content="Corrige el candidato rechazado")
    baseline = CandidateRoutingProvider([PLAN_V3, FIXED, YES])
    assert execute(store, config, baseline_id, baseline).status == "approved"
    expected_planner = next(payload for role, payload in baseline.calls if role == "planner")
    expected_coder = next(payload for role, payload in baseline.calls if role == "coder")

    conversation, _, candidate_id = _approved_then_exhausted(store, config)
    _, run_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                       content="Corrige el candidato rechazado")
    with store() as db:
        claimed, lease = claim_next(db, config)
        assert claimed == run_id
    with pytest.raises(_Crash):
        process_run(store, run_id, config, CrashAfterCandidateRoute([]), lease)
    with SqliteSaver.from_conn_string(config.langgraph_sqlite_path) as checkpointer:
        checkpoint = checkpointer.get_tuple({"configurable": {"thread_id": run_id}})
        assert checkpoint.checkpoint["channel_values"]["previous_files"] == CODE["files"]
        assert checkpoint.checkpoint["channel_values"]["candidate_files"] == REJECTED["files"]
    with store() as db:
        run = db.get(Run, run_id)
        assert run.base_snapshot_id == candidate_id
        run.heartbeat_at = now() - timedelta(seconds=config.job_stale_seconds + 1)
        db.commit()
    with store() as db:
        claimed, recovered_lease = claim_next(db, config)
        assert claimed == run_id
        assert db.get(Run, run_id).base_snapshot_id == candidate_id
        assert snapshot_files(db, candidate_id) == REJECTED["files"]
    resumed = CandidateRoutingProvider([PLAN_V3, FIXED, YES])
    process_run(store, run_id, config, resumed, recovered_lease)
    assert "router" not in [role for role, _ in resumed.calls]
    resumed_planner = next(payload for role, payload in resumed.calls if role == "planner")
    resumed_coder = next(payload for role, payload in resumed.calls if role == "coder")
    assert resumed_planner["files"] == expected_planner["files"]
    assert resumed_coder["previous_files"] == expected_coder["previous_files"]
    with SqliteSaver.from_conn_string(config.langgraph_sqlite_path) as checkpointer:
        checkpoint = checkpointer.get_tuple({"configurable": {"thread_id": run_id}})
        # process_run ofreció REJECTED como nueva base, pero LangGraph conservó el estado anterior.
        assert checkpoint.checkpoint["channel_values"]["previous_files"] == CODE["files"]
    with store() as db:
        run = db.get(Run, run_id)
        snapshot = db.get(Snapshot, run.result_snapshot_id)
        assert run.status == "approved"
        assert run.base_snapshot_id == candidate_id
        assert snapshot.parent_snapshot_id == candidate_id
        assert len(db.scalars(select(Trace).where(
            Trace.run_id == run_id, Trace.stage == "router")).all()) == 1


def test_followup_context_has_no_file_or_criteria_fallback_without_result_snapshots(store, config):
    conversation, _, _ = _approved_then_exhausted(store, config)
    _, run_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                       content="Corrige el candidato rechazado")
    with store() as db:
        prior = db.scalars(select(Run).where(Run.conversation_id == conversation, Run.id != run_id)).all()
        assert {item.status for item in prior} == {"approved", "exhausted"}
        for item in prior:
            item.result_snapshot_id = None
        db.flush()
        context = followup_context(db, db.get(Run, run_id))
        assert context["candidate_files"] == []
        assert context["candidate_criteria"] == []
        assert context["approved_criteria"] == []
        assert context["candidate_feedback"] == ["Fix output"]


def test_turn_ledger_is_bounded_and_uses_request_first_lines(store):
    conversation = None
    last_id = None
    for number in range(8):
        conversation, last_id = queued(
            store, conversation=(Conversation(id=conversation, owner_hash=owner_hash("alice"))
                                 if conversation else None),
            content=f"Turn {number}\nextra detail")
    with store() as db:
        ledger = turn_ledger(db, db.get(Run, last_id))
    assert len(ledger) == 6
    assert [item["request"] for item in ledger] == [f"Turn {number}" for number in range(1, 7)]
    assert all(len(item["request"]) <= 200 for item in ledger)
