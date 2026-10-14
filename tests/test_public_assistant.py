from copy import deepcopy
from uuid import uuid4

import pytest

from tests.test_public_accounts import GUEST_PASSWORD, ORIGIN, OWNER_PASSWORD
from tests.test_public_store import MemoryBackend
from tradingagents.workbench.public_accounts import EmailAuth
from tradingagents.workbench.public_store import EncryptedStore

NOW = 1_800_000_000


class History:
    def __init__(self):
        self.reads = []
        self.writes = []
        self.records = [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "request": {"ticker": "MSFT", "analysis_date": "2026-09-08", "mode": "analysis"},
                "status": "succeeded",
                "created_at": "2026-09-08T08:00:00Z",
                "result": {"reports": {"market_report": "MSFT market rose 2 percent."}, "stats": {"llm_calls": 7}},
                "events": [],
            },
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "request": {"ticker": "VOO", "analysis_date": "2026-09-07", "mode": "analysis"},
                "status": "failed",
                "created_at": "2026-09-07T08:00:00Z",
                "result": None,
                "events": [],
            },
        ]

    def list(self, session_id):
        self.reads.append(("list", session_id))
        return [{key: record[key] for key in ("id", "request", "status", "created_at")} for record in self.records]

    def get(self, session_id, run_id):
        self.reads.append(("get", session_id, run_id))
        return deepcopy(next(record for record in self.records if record["id"] == run_id))


class ChatGateway:
    def __init__(self):
        self.calls = []

    def managed_identity_available(self):
        return True

    def assistant_reply(self, principal, conversation_id, messages, *, context):
        self.calls.append((principal, conversation_id, deepcopy(messages), context))
        return "MSFT research history says the market report noted a 2% move. [run:11111111-1111-4111-8111-111111111111]"


@pytest.fixture
def assistant_case():
    backend = MemoryBackend()
    store = EncryptedStore(backend, b"a" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    invite = auth.invite(owner.session_id, "guest@example.com")
    guest = auth.redeem(invite.token, invite.email, GUEST_PASSWORD)
    history = History()
    gateway = ChatGateway()
    from tradingagents.workbench.public_assistant import ResearchAssistant

    assistant = ResearchAssistant(store=store, auth=auth, runs=history, gateway=gateway, clock=lambda: NOW)
    return {
        "assistant": assistant,
        "backend": backend,
        "gateway": gateway,
        "guest": guest,
        "history": history,
        "owner": owner,
    }


def test_assistant_sessions_are_encrypted_account_scoped_and_mutable(assistant_case):
    from tradingagents.workbench.public_assistant import AssistantError

    assistant = assistant_case["assistant"]
    owner = assistant_case["owner"]
    guest = assistant_case["guest"]

    created = assistant.create(owner.session_id)
    assert created["title"] == "新对话"
    assert assistant.list(owner.session_id) == [created]
    assistant.rename(owner.session_id, created["id"], "MSFT 分析")
    assert assistant.get(owner.session_id, created["id"])["title"] == "MSFT 分析"
    assert assistant.list(guest.session_id) == []
    guest_created = assistant.create(guest.session_id)
    assert assistant.list(guest.session_id) == [guest_created]
    with pytest.raises(AssistantError) as hidden:
        assistant.get(guest.session_id, created["id"])
    assert hidden.value.status_code == 404

    key = owner.principal.owner_key + "/research-assistant"
    ciphertext = assistant_case["backend"].records[key][0]
    assert b"MSFT" not in ciphertext
    assistant.delete(owner.session_id, created["id"])
    assert assistant.list(owner.session_id) == []


def test_assistant_session_write_retries_a_compare_and_swap_conflict(assistant_case):
    from tradingagents.workbench.public_store import StoreConflict

    backend = assistant_case["backend"]
    original_write = backend.write
    conflict = [True]

    def conflict_once(key, content, expected_version=None):
        if key.endswith("/research-assistant") and conflict[0]:
            conflict[0] = False
            raise StoreConflict
        return original_write(key, content, expected_version)

    backend.write = conflict_once
    created = assistant_case["assistant"].create(assistant_case["owner"].session_id)
    assert created["id"]
    assert conflict == [False]
    assert len(assistant_case["assistant"].list(assistant_case["owner"].session_id)) == 1


def test_assistant_turn_reads_only_the_current_accounts_history_and_is_idempotent(assistant_case):
    assistant = assistant_case["assistant"]
    owner = assistant_case["guest"]
    session = assistant.create(owner.session_id)
    turn_id = str(uuid4())

    result = assistant.send(owner.session_id, session["id"], turn_id, "What did the MSFT report say?")
    repeated = assistant.send(owner.session_id, session["id"], turn_id, "What did the MSFT report say?")

    assert result == repeated
    assert [message["role"] for message in result["messages"]] == ["user", "assistant"]
    assert "What did the MSFT report say?" in assistant_case["gateway"].calls[0][2][0]["content"]
    assert "MSFT market rose 2 percent" in assistant_case["gateway"].calls[0][3]
    assert "11111111-1111-4111-8111-111111111111" in assistant_case["gateway"].calls[0][3]
    assert assistant_case["gateway"].calls[0][1] == session["id"]
    assert len(assistant_case["gateway"].calls) == 1
    assert all(read[1] == owner.session_id for read in assistant_case["history"].reads)
    assert assistant_case["history"].writes == []


def test_uncertain_assistant_turn_is_saved_as_error_and_is_not_automatically_replayed(assistant_case):
    from tradingagents.workbench.public_assistant import AssistantError

    gateway = assistant_case["gateway"]

    def uncertain(*args, **kwargs):
        gateway.calls.append((args, kwargs))
        raise RuntimeError("upstream outcome unknown")

    gateway.assistant_reply = uncertain
    assistant = assistant_case["assistant"]
    owner = assistant_case["owner"]
    session = assistant.create(owner.session_id)
    turn_id = str(uuid4())
    with pytest.raises(RuntimeError):
        assistant.send(owner.session_id, session["id"], turn_id, "What happened to MSFT?")
    saved = assistant.get(owner.session_id, session["id"])
    assert saved["messages"][-1]["status"] == "error"
    with pytest.raises(AssistantError):
        assistant.send(owner.session_id, session["id"], turn_id, "What happened to MSFT?")
    assert len(gateway.calls) == 1


def test_history_context_failure_marks_turn_error_without_calling_luna(assistant_case):
    from tradingagents.workbench.public_assistant import AssistantError

    assistant = assistant_case["assistant"]
    session_id = assistant_case["owner"].session_id
    session = assistant.create(session_id)
    assistant_case["history"].list = lambda _session_id: (_ for _ in ()).throw(RuntimeError("storage unavailable"))
    turn_id = str(uuid4())

    with pytest.raises(AssistantError) as failure:
        assistant.send(session_id, session["id"], turn_id, "Summarize my history")

    assert failure.value.status_code == 503
    saved = assistant.get(session_id, session["id"])
    assert saved["messages"][-1]["status"] == "error"
    assert assistant_case["gateway"].calls == []


@pytest.mark.parametrize("message", ["", " ", "x" * 4001, "\x00invalid"])
def test_assistant_rejects_invalid_or_oversized_user_messages(assistant_case, message):
    from tradingagents.workbench.public_assistant import AssistantError

    assistant = assistant_case["assistant"]
    session = assistant.create(assistant_case["owner"].session_id)
    with pytest.raises(AssistantError):
        assistant.send(assistant_case["owner"].session_id, session["id"], str(uuid4()), message)
    assert assistant_case["gateway"].calls == []


def test_assistant_enforces_session_cap_without_evicting_saved_conversations(assistant_case):
    from tradingagents.workbench.public_assistant import AssistantError

    assistant = assistant_case["assistant"]
    session_id = assistant_case["owner"].session_id
    sessions = [assistant.create(session_id) for _ in range(12)]
    with pytest.raises(AssistantError) as failure:
        assistant.create(session_id)
    assert failure.value.status_code == 429
    assert {session["id"] for session in assistant.list(session_id)} == {session["id"] for session in sessions}
