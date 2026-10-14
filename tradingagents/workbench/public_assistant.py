"""Encrypted, account-scoped research assistant conversations."""

import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from tradingagents.workbench.public_accounts import AccountError
from tradingagents.workbench.public_store import StoreConflict, StoreUnavailable
from tradingagents.workbench.public_usage import UsageLedger

_MAX_SESSIONS = 12
_MAX_MESSAGES = 40
_MAX_MESSAGE_CHARS = 4000
_MAX_TITLE_CHARS = 80
_MAX_CONTEXT_RUNS = 8
_MAX_CONTEXT_CHARS = 18_000
_CAS_ATTEMPTS = 6
_TURN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class AssistantError(RuntimeError):
    def __init__(self, message="研究助手请求无法完成。", *, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _clean_text(value, *, maximum):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise AssistantError()
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise AssistantError()
    return value.strip()


class ResearchAssistant:
    def __init__(self, *, store, auth, runs, gateway, clock=time.time, usage=None):
        self._store = store
        self._auth = auth
        self._runs = runs
        self._gateway = gateway
        self._clock = clock
        self._usage = usage if usage is not None else UsageLedger(store, clock=clock)

    def _principal(self, session_id):
        try:
            principal = self._auth.session(session_id).principal
        except Exception:
            raise AssistantError("登录状态已失效。", status_code=401) from None
        try:
            self._auth.require_feature(principal, "assistant")
        except AccountError as error:
            raise AssistantError("Account operation is not permitted.", status_code=error.status_code) from None
        return principal

    @staticmethod
    def _key(principal):
        return principal.owner_key + "/research-assistant"

    def _read(self, principal):
        try:
            stored = self._store.read(self._key(principal))
        except StoreUnavailable:
            raise AssistantError("聊天记录暂时无法读取。", status_code=503) from None
        if stored is None:
            return {"version": 1, "sessions": []}, None
        record, version = stored
        try:
            self._validate_record(record)
            if type(version) is not str or not version:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AssistantError("聊天记录格式无效。", status_code=503) from None
        return record, version

    @staticmethod
    def _validate_record(record):
        if (type(record) is not dict or set(record) != {"version", "sessions"}
                or record["version"] != 1 or type(record["sessions"]) is not list
                or len(record["sessions"]) > _MAX_SESSIONS):
            raise ValueError
        session_ids = set()
        for session in record["sessions"]:
            if (type(session) is not dict or set(session) != {"id", "title", "created_at", "updated_at", "messages"}
                    or not _valid_uuid(session["id"]) or session["id"] in session_ids
                    or type(session["title"]) is not str or len(session["title"]) > _MAX_TITLE_CHARS
                    or type(session["created_at"]) is not str or type(session["updated_at"]) is not str
                    or type(session["messages"]) is not list or len(session["messages"]) > _MAX_MESSAGES):
                raise ValueError
            session_ids.add(session["id"])
            turn_ids = set()
            for message in session["messages"]:
                if (type(message) is not dict or set(message) != {"turn_id", "role", "content", "created_at", "status"}
                        or not _valid_uuid(message["turn_id"])
                        or message["role"] not in {"user", "assistant"}
                        or type(message["content"]) is not str or not 1 <= len(message["content"]) <= _MAX_MESSAGE_CHARS
                        or type(message["created_at"]) is not str
                        or message["status"] not in {"pending", "complete", "error"}):
                    raise ValueError
                if message["role"] == "user":
                    if message["status"] not in {"pending", "complete", "error"}:
                        raise ValueError
                    turn_ids.add(message["turn_id"])
                elif message["turn_id"] not in turn_ids or message["status"] != "complete":
                    raise ValueError

    def _mutate(self, principal, operation):
        for _ in range(_CAS_ATTEMPTS):
            record, version = self._read(principal)
            result = operation(record)
            try:
                self._store.write(self._key(principal), record, version)
                return deepcopy(result)
            except StoreConflict:
                continue
            except StoreUnavailable:
                raise AssistantError("聊天记录无法保存。", status_code=503) from None
        raise AssistantError("聊天记录正在被其他操作更新，请重试。", status_code=409)

    @staticmethod
    def _summary(session):
        return {name: session[name] for name in ("id", "title", "created_at", "updated_at")}

    def list(self, session_id):
        principal = self._principal(session_id)
        record, _ = self._read(principal)
        return [self._summary(session) for session in sorted(
            record["sessions"], key=lambda item: (item["updated_at"], item["id"]), reverse=True,
        )]

    def create(self, session_id):
        principal = self._principal(session_id)
        now = _iso(self._clock())
        identifier = str(uuid4())

        def create_record(record):
            if len(record["sessions"]) >= _MAX_SESSIONS:
                raise AssistantError("对话数量已达上限，请删除旧对话后再新建。", status_code=429)
            session = {"id": identifier, "title": "新对话", "created_at": now, "updated_at": now, "messages": []}
            record["sessions"].append(session)
            return self._summary(session)

        return self._mutate(principal, create_record)

    def get(self, session_id, assistant_session_id):
        principal = self._principal(session_id)
        if not _valid_uuid(assistant_session_id):
            raise AssistantError("对话不存在。", status_code=404)
        record, _ = self._read(principal)
        session = next((item for item in record["sessions"] if item["id"] == assistant_session_id), None)
        if session is None:
            raise AssistantError("对话不存在。", status_code=404)
        return deepcopy(session)

    def rename(self, session_id, assistant_session_id, title):
        principal = self._principal(session_id)
        if not _valid_uuid(assistant_session_id):
            raise AssistantError("对话不存在。", status_code=404)
        title = _clean_text(title, maximum=_MAX_TITLE_CHARS)

        def rename_record(record):
            session = next((item for item in record["sessions"] if item["id"] == assistant_session_id), None)
            if session is None:
                raise AssistantError("对话不存在。", status_code=404)
            session["title"] = title
            session["updated_at"] = _iso(self._clock())
            return self._summary(session)

        return self._mutate(principal, rename_record)

    def delete(self, session_id, assistant_session_id):
        principal = self._principal(session_id)
        if not _valid_uuid(assistant_session_id):
            raise AssistantError("对话不存在。", status_code=404)

        def delete_record(record):
            remaining = [item for item in record["sessions"] if item["id"] != assistant_session_id]
            if len(remaining) == len(record["sessions"]):
                raise AssistantError("对话不存在。", status_code=404)
            record["sessions"] = remaining
            return {"deleted": True}

        return self._mutate(principal, delete_record)

    def _history_context(self, session_id, query):
        principal = self._auth.session(session_id).principal
        if not self._auth.feature_permissions(principal)["research"]:
            return ""
        try:
            summaries = self._runs.list(session_id)
        except Exception:
            raise AssistantError("运行历史暂时无法读取。", status_code=503) from None
        terms = {term.lower() for term in re.findall(r"[A-Za-z0-9.%-]{2,}", query)}
        ranked = []
        for summary in summaries[:100]:
            request = summary.get("request", {})
            settings = summary.get("settings", {})
            searchable = " ".join((
                str(summary.get("id", "")),
                *(str(request.get(field, "")) for field in ("ticker", "analysis_date", "mode")),
                *(str(settings.get(field, "")) for field in ("quick_model", "deep_model")),
            ))
            score = sum(1 for term in terms if term in searchable.lower())
            ranked.append((score, summary.get("created_at", ""), summary.get("id"), summary))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [item[3] for item in ranked[:_MAX_CONTEXT_RUNS]]
        context = []
        remaining = _MAX_CONTEXT_CHARS
        for summary in selected:
            try:
                run = self._runs.get(session_id, summary["id"])
            except Exception:
                continue
            request = run.get("request", {})
            settings = run.get("settings", {})
            reports = (run.get("result") or {}).get("reports") or {}
            stats = (run.get("result") or {}).get("stats") or {}
            report_text = "\n".join(f"{name}: {content}" for name, content in reports.items() if type(content) is str)
            entry = (
                f"RUN {run.get('id')} | ticker={request.get('ticker')} | date={request.get('analysis_date')} "
                f"| status={run.get('status')} | model={settings.get('quick_model') or settings.get('deep_model')} "
                f"| model_calls={stats.get('llm_calls')} | tool_calls={stats.get('tool_calls')}\n{report_text}"
            )
            excerpt = entry[: min(2600, remaining)]
            if excerpt:
                context.append(excerpt)
                remaining -= len(excerpt)
            if remaining <= 0:
                break
        return "\n\n".join(context)

    def send(self, session_id, assistant_session_id, turn_id, content):
        principal = self._principal(session_id)
        if not _valid_uuid(assistant_session_id) or type(turn_id) is not str or not _TURN_ID.fullmatch(turn_id):
            raise AssistantError()
        content = _clean_text(content, maximum=_MAX_MESSAGE_CHARS)
        existing = self.get(session_id, assistant_session_id)
        turn_messages = [message for message in existing["messages"] if message["turn_id"] == turn_id]
        if turn_messages:
            prior_user = next((message for message in turn_messages if message["role"] == "user"), None)
            if prior_user is None or prior_user["content"] != content:
                raise AssistantError("该发送编号已用于其他消息。", status_code=409)
            prior_answer = next((message for message in turn_messages if message["role"] == "assistant"), None)
            if prior_answer is not None:
                answer = prior_answer
                return {**self._summary(existing), "messages": existing["messages"], "answer": answer["content"]}
            raise AssistantError("这条消息尚未完成，请新建一条重试。", status_code=409)

        now = _iso(self._clock())

        def append_user(record):
            session = next((item for item in record["sessions"] if item["id"] == assistant_session_id), None)
            if session is None:
                raise AssistantError("对话不存在。", status_code=404)
            existing_turn = next((item for item in session["messages"] if item["turn_id"] == turn_id), None)
            if existing_turn is not None:
                if existing_turn["role"] != "user" or existing_turn["content"] != content:
                    raise AssistantError("该发送编号已用于其他消息。", status_code=409)
                raise AssistantError("这条消息尚未完成，请新建一条重试。", status_code=409)
            if len(session["messages"]) + 2 > _MAX_MESSAGES:
                raise AssistantError("对话消息已达上限，请新建对话继续。", status_code=409)
            session["messages"].append({"turn_id": turn_id, "role": "user", "content": content, "created_at": now, "status": "pending"})
            session["updated_at"] = now
            return deepcopy(session)

        session = self._mutate(principal, append_user)
        attempted = False
        try:
            context = self._history_context(session_id, content)
            conversation = session["messages"][:-1][-17:]
            if conversation and conversation[0]["role"] == "assistant":
                conversation = conversation[1:]
            self._principal(session_id)
            attempted = True
            response = self._gateway.assistant_reply(
                principal,
                assistant_session_id,
                [{"role": item["role"], "content": item["content"]} for item in conversation] + [{"role": "user", "content": content}],
                context=context,
            )
            answer = _clean_text(response, maximum=_MAX_MESSAGE_CHARS)
        except Exception as error:
            if attempted:
                self._usage.capture(principal.object_id, "assistant:" + turn_id, "assistant", success=False)
            failed_at = _iso(self._clock())

            def fail_turn(record):
                target = next((item for item in record["sessions"] if item["id"] == assistant_session_id), None)
                if target is None:
                    raise AssistantError("对话不存在。", status_code=404)
                user_message = next((item for item in target["messages"] if item["turn_id"] == turn_id and item["role"] == "user"), None)
                if user_message is not None and user_message["status"] == "pending":
                    user_message["status"] = "error"
                    target["updated_at"] = failed_at
                return self._summary(target)

            self._mutate(principal, fail_turn)
            raise AssistantError("Research assistant request could not be completed.",
                                 status_code=getattr(error, "status_code", 502)) from None
        self._usage.capture(principal.object_id, "assistant:" + turn_id, "assistant", success=True)
        completed_at = _iso(self._clock())

        def complete_turn(record):
            target = next((item for item in record["sessions"] if item["id"] == assistant_session_id), None)
            if target is None:
                raise AssistantError("对话不存在。", status_code=404)
            user_message = next((item for item in target["messages"] if item["turn_id"] == turn_id and item["role"] == "user"), None)
            if user_message is None or user_message["status"] != "pending":
                raise AssistantError("对话状态已变化。", status_code=409)
            user_message["status"] = "complete"
            target["messages"].append({"turn_id": turn_id, "role": "assistant", "content": answer, "created_at": completed_at, "status": "complete"})
            if target["title"] == "新对话":
                target["title"] = content[:_MAX_TITLE_CHARS].strip()
            target["updated_at"] = completed_at
            return deepcopy(target)

        completed = self._mutate(principal, complete_turn)
        return {**self._summary(completed), "messages": completed["messages"], "answer": answer}
