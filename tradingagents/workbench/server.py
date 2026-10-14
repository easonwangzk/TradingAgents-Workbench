"""Loopback-only workbench HTTP API and recoverable local run lifecycle."""

import argparse
import copy
import json
import math
import mimetypes
import os
import secrets
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from tradingagents.workbench import contracts
from tradingagents.workbench.azure_control import (
    MAX_LOG_BYTES,
    MODEL,
    AzureController,
    validate_config,
    validate_run_id,
)

ROOT = Path(__file__).resolve().parents[2]
STATE_ROOT = ROOT / ".local-run" / "workbench"
ACTIVE = {"starting", "running", "collecting"}
STATUSES = ACTIVE | {"succeeded", "failed", "stopped"}
POLL_SECONDS = 5
INGESTION_SECONDS = 600
BODY_LIMIT = 16384
START_ERROR = "Azure start could not be confirmed. Check Job executions before retrying."
POLL_ERROR = "Azure status or logs are temporarily unavailable. Refresh to retry."
STOP_ERROR = "Azure stop could not be confirmed. The run remains reserved; refresh to retry."
WORKER_ERROR = "The worker reported an error. Review available reports and deployment health."


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def _valid_result(value):
    if type(value) is not dict or set(value) != {"decision", "reports", "stats", "connectivity"}:
        return False
    if value["decision"] is not None and not isinstance(value["decision"], str):
        return False
    if type(value["reports"]) is not dict or any(
        not isinstance(section, str) or not isinstance(content, str)
        for section, content in value["reports"].items()
    ):
        return False
    stats = value["stats"]
    if type(stats) is not dict or any(
        type(stats.get(field)) is not int or stats[field] < 0
        for field in ("llm_calls", "tool_calls", "input_tokens", "output_tokens")
    ):
        return False
    return _valid_connectivity(value["connectivity"])


def _valid_connectivity(rows):
    return isinstance(rows, list) and all(
        type(row) is dict and all(isinstance(row.get(field), str) for field in ("provider", "status", "detail"))
        and type(row.get("elapsed_seconds")) in (int, float)
        and math.isfinite(row["elapsed_seconds"]) and row["elapsed_seconds"] >= 0
        for row in rows
    )


class RunService:
    def __init__(self, directory, controller, *, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cloud = controller
        self.clock = clock
        self.lock = threading.RLock()
        self.records = {}
        self.operations = {}
        self.scheduled = set()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="workbench")
        for path in self.directory.glob("*.json"):
            try:
                validate_run_id(path.stem)
                if path.stat().st_size > 3 * MAX_LOG_BYTES:
                    continue
                record = json.loads(path.read_bytes())
                if (record["id"] != path.stem or record["status"] not in STATUSES
                        or not isinstance(record["events"], list)):
                    continue
                record["request"] = contracts.validate_request(record["request"])
                if record.get("result") is not None and not _valid_result(record["result"]):
                    record["result"] = None
                if not record.get("execution_name") and (
                    record["status"] in ACTIVE or record.get("start_pending") or record.get("stop_pending")
                ):
                    stopped = record["status"] == "stopped" or record.get("stop_requested")
                    uncertain = bool(record.get("start_attempted"))
                    record.update(
                        status="stopped" if stopped else "failed", start_pending=False,
                        stop_pending=False, start_uncertain=uncertain,
                        error=START_ERROR if uncertain else None if stopped else "Local server stopped before run submission.",
                    )
                if record["status"] == "succeeded" and record.get("result") is None:
                    record.update(status="collecting", collection_started_at=self.clock())
                record["start_pending"] = False
                record["last_poll_at"] = None
                self.records[path.stem] = contracts.scrub_secrets(record)
                self.operations[path.stem] = threading.Lock()
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                continue

    def close(self):
        self.executor.shutdown(wait=True)

    def _save(self, record):
        temporary = None
        try:
            data = _json_bytes(contracts.scrub_secrets(record))
            if len(data) > 3 * MAX_LOG_BYTES:
                raise ValueError
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.directory, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / f"{record['id']}.json")
        except (OSError, ValueError):
            raise HTTPException(503, "Local run record could not be saved.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _record(self, run_id):
        try:
            validate_run_id(run_id)
        except ValueError:
            raise HTTPException(400, "Invalid run identifier.") from None
        if run_id not in self.records:
            raise HTTPException(404, "Run not found.")
        return self.records[run_id]

    def get(self, run_id):
        with self.lock:
            return copy.deepcopy(self._record(run_id))

    def list_runs(self):
        with self.lock:
            summaries = [
                {**copy.deepcopy({key: value for key, value in record.items() if key not in {"events", "result"}}),
                 "events": [], "result": None, "has_result": record.get("result") is not None}
                for record in self.records.values()
            ]
            return sorted(summaries, key=lambda record: record["created_at"], reverse=True)

    def create(self, payload):
        try:
            request = contracts.validate_request(payload)
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid run request.") from None
        with self.lock:
            if self.cloud is None:
                raise HTTPException(503, "Workbench deployment is not configured.")
            if any(
                record["status"] in ACTIVE or record.get("start_pending")
                or record.get("stop_pending") or record.get("start_uncertain")
                for record in self.records.values()
            ):
                raise HTTPException(409, "Another run is active or awaiting a confirmed stop.")
            record = {
                "id": str(uuid4()), "request": request, "status": "starting",
                "created_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                "execution_name": None, "events": [], "result": None, "error": None,
                "start_pending": True, "stop_pending": False, "stop_requested": False, "start_uncertain": False,
                "last_poll_at": None, "poll_error": None, "collection_started_at": None,
                "log_warning": None,
            }
            self._save(record)
            self.records[record["id"]] = record
            self.operations[record["id"]] = threading.Lock()
            return copy.deepcopy(record)

    def schedule(self, operation, run_id):
        key = (operation, run_id)
        with self.lock:
            if key in self.scheduled:
                return
            self.scheduled.add(key)

        def execute():
            try:
                getattr(self, operation)(run_id)
            except Exception:
                with self.lock:
                    self.records[run_id]["error"] = "Local run update failed. Refresh to retry."
            finally:
                with self.lock:
                    self.scheduled.discard(key)

        self.executor.submit(execute)

    def start(self, run_id):
        with self.operations[run_id]:
            with self.lock:
                record = self._record(run_id)
                if not record.get("start_pending"):
                    return
                if record.get("stop_requested"):
                    record.update(status="stopped", start_pending=False, stop_pending=False)
                    self._save(record)
                    return
                record["start_attempted"] = True
                self._save(record)
                request = copy.deepcopy(record["request"])
            try:
                execution = self.cloud.start_run(run_id, request)
            except Exception as failure:
                uncertain = getattr(failure, "execution_may_exist", True)
                with self.lock:
                    record.update(status="stopped" if record.get("stop_requested") else "failed",
                                  error=START_ERROR if uncertain else "Azure Job could not be started. Check deployment and Azure CLI access.",
                                  start_pending=False, stop_pending=False, start_uncertain=uncertain)
                    self._save(record)
                return
            with self.lock:
                record.update(execution_name=execution, start_pending=False,
                              status="stopped" if record.get("stop_requested") else "running")
                self._save(record)
            self._stop_locked(record)

    def request_stop(self, run_id):
        with self.lock:
            record = self._record(run_id)
            if record["status"] in ACTIVE:
                record.update(status="stopped", stop_requested=True, stop_pending=True, error=None)
                self._save(record)
            return copy.deepcopy(record)

    def _stop_locked(self, record):
        with self.lock:
            if not record.get("stop_pending") or not record.get("execution_name"):
                return
            execution = record["execution_name"]
            accepted = record.get("stop_accepted", False)
        if not accepted:
            try:
                self.cloud.stop_run(execution)
            except Exception:
                with self.lock:
                    record["error"] = STOP_ERROR
            else:
                with self.lock:
                    record.update(stop_accepted=True, error=None)
        try:
            status = self.cloud.get_status(execution)
        except Exception:
            with self.lock:
                record["poll_error"] = POLL_ERROR
        else:
            with self.lock:
                record["poll_error"] = None
                if status in {"Succeeded", "Failed", "Stopped"}:
                    record.update(status="stopped", stop_pending=False, error=None)
        with self.lock:
            record["last_poll_at"] = self.clock()
            self._save(record)

    def stop(self, run_id):
        with self.operations[run_id]:
            self._stop_locked(self._record(run_id))

    def refresh(self, run_id):
        self.get(run_id)
        operation = self.operations[run_id]
        if not operation.acquire(blocking=False):
            return self.get(run_id)
        try:
            with self.lock:
                record = self._record(run_id)
                now = self.clock()
                previous_poll = record.get("last_poll_at")
                if (self.cloud is None or not record.get("execution_name")
                        or (record["status"] not in ACTIVE and record.get("result") is not None
                            and not record.get("stop_pending") and not record.get("log_warning"))
                        or (previous_poll is not None and now - previous_poll < POLL_SECONDS)):
                    return copy.deepcopy(record)
                record["last_poll_at"] = now
                execution = record["execution_name"]
                active = record["status"] in ACTIVE
            self._stop_locked(record)
            status = None
            with self.lock:
                poll_error = record.get("poll_error") if record.get("stop_pending") else None
                log_warning = record.get("log_warning")
            if active:
                try:
                    status = self.cloud.get_status(execution)
                except Exception:
                    poll_error = POLL_ERROR
            try:
                warnings = []
                events = self.cloud.get_events(run_id, warnings=warnings)
                log_warning = warnings[0] if warnings else None
            except Exception:
                events = []
                poll_error = POLL_ERROR
            with self.lock:
                combined = {_json_bytes(event): event for event in record["events"]}
                for event in contracts.scrub_secrets(events):
                    if event["kind"] == "error":
                        event["payload"] = {"message": WORKER_ERROR}
                    combined[_json_bytes(event)] = event
                if sum(len(key) for key in combined) > MAX_LOG_BYTES:
                    poll_error = "Run events exceed the local size limit."
                else:
                    record["events"] = sorted(combined.values(), key=lambda event: event["timestamp"])
                for event in record["events"]:
                    if event["kind"] == "result" and _valid_result(event["payload"]):
                        record["result"] = event["payload"]
                record["poll_error"] = poll_error
                record["log_warning"] = log_warning
                if record["status"] in ACTIVE:
                    record["error"] = poll_error
                    if status == "Failed":
                        record.update(status="failed", error="Azure Job execution failed. Review available reports.")
                    elif status == "Stopped":
                        record.update(status="stopped", error=None)
                    elif status == "Succeeded" or record["status"] == "collecting":
                        if record["result"] is not None:
                            record.update(status="succeeded", error=None)
                        else:
                            record["status"] = "collecting"
                            if record.get("collection_started_at") is None:
                                record["collection_started_at"] = now
                            if now - record["collection_started_at"] >= INGESTION_SECONDS:
                                record.update(status="failed", error="No validated result arrived after 10 minutes of log ingestion.")
                self._save(record)
                return copy.deepcopy(record)
        finally:
            operation.release()


class LocalAccessMiddleware:
    def __init__(self, app, *, port, token):
        self.app = app
        self.hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        self.token = token.encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {}
        for name, value in scope["headers"]:
            headers.setdefault(name.lower(), []).append(value.decode("latin-1"))
        hosts = headers.get(b"host", [])
        origins = headers.get(b"origin", [])
        host = hosts[0] if len(hosts) == 1 else ""
        status = None
        if host not in self.hosts:
            status = 400
        elif origins and origins != ["http://" + host]:
            status = 403
        write = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        if write:
            tokens = headers.get(b"x-workbench-token", [])
            if (origins != ["http://" + host] or len(tokens) != 1
                    or not secrets.compare_digest(tokens[0].encode("latin-1"), self.token)):
                status = 403
        if status is not None:
            await JSONResponse({"detail": "Local request origin or authorization is invalid."}, status)(scope, receive, send)
            return
        body = bytearray()
        if write:
            lengths = headers.get(b"content-length", [])
            if lengths and (len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) > BODY_LIMIT):
                await JSONResponse({"detail": "Request body exceeds the local size limit."}, 413)(scope, receive, send)
                return
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > BODY_LIMIT:
                    await JSONResponse({"detail": "Request body exceeds the local size limit."}, 413)(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break

        async def body_receive():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"),
                ]
            await send(message)

        try:
            await self.app(scope, body_receive if write else receive, safe_send)
        except Exception:
            if not started:
                await JSONResponse({"detail": "Local workbench request failed."}, 500)(scope, receive, send)


def _markdown(record):
    reports = {}
    connectivity = []
    for event in record["events"]:
        payload = event.get("payload", {})
        rows = payload.get("connectivity")
        if event.get("kind") == "connectivity" and rows and _valid_connectivity(rows):
            connectivity = rows
        if (event.get("kind") == "report" and isinstance(payload.get("section"), str)
                and isinstance(payload.get("content"), str)):
            reports[payload["section"]] = payload["content"]
    result = record.get("result") or {}
    reports.update(result.get("reports", {}))
    connectivity = result.get("connectivity") or connectivity
    sections = [f"# {record['request']['ticker']} workbench run", f"Run: {record['id']}",
                f"Status: {record['status']}", f"Date: {record['request']['analysis_date']}"]
    if result.get("decision"):
        sections.append("## Decision\n\n" + result["decision"])
    sections.extend(f"## {section}\n\n{content}" for section, content in reports.items())
    if connectivity:
        sections.append("## Connectivity\n\n" + "\n\n".join(
            f"{row['provider']}: {row['status']} ({row['elapsed_seconds']}s)\n\n{row['detail']}"
            for row in connectivity
        ))
    if result.get("stats"):
        sections.append("## Usage\n\n" + "\n".join(f"- {key}: {value}" for key, value in result["stats"].items()))
    if record.get("error"):
        sections.append("## Error\n\n" + record["error"])
    return "\n\n".join(sections) + "\n"


def create_app(*, config_path=None, state_dir=None, port=8765, controller=None, clock=time.time):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid local server port")
    path = Path(config_path or os.environ.get("TRADINGAGENTS_WORKBENCH_CONFIG") or STATE_ROOT / "deployment.json")
    deployment = None
    try:
        if path.stat().st_size <= BODY_LIMIT:
            deployment = validate_config(json.loads(path.read_bytes()))
    except (OSError, ValueError, TypeError):
        pass
    cloud = (controller or AzureController(deployment)) if deployment is not None else None
    service = RunService(state_dir or STATE_ROOT / "runs", cloud, clock=clock)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await run_in_threadpool(service.close)
        if cloud is not None and controller is None:
            cloud.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.add_middleware(LocalAccessMiddleware, port=port, token=token)
    static = Path(__file__).parent / "static"
    mimetypes.add_type("text/javascript", ".mjs")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/api/config")
    def config():
        return {
            "defaults": contracts.default_request(),
            "analyst_choices": [{"value": name, "label": name} for name in contracts.ANALYSTS],
            "vendor_choices": {category: list(vendors) for category, vendors in contracts.VENDOR_OPTIONS.items()},
            "model": MODEL, "deployment_ready": deployment is not None, "csrf_token": token,
        }

    @app.get("/api/runs")
    def list_runs(background_tasks: BackgroundTasks):
        records = service.list_runs()
        for record in records:
            background_tasks.add_task(service.schedule, "refresh", record["id"])
        return {"runs": records}

    @app.post("/api/runs", status_code=202)
    async def create_run(request: Request, background_tasks: BackgroundTasks):
        try:
            payload = json.loads(await request.body())
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid JSON request.") from None
        record = await run_in_threadpool(service.create, payload)
        background_tasks.add_task(service.schedule, "start", record["id"])
        return record

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return service.refresh(run_id)

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str, background_tasks: BackgroundTasks):
        record = service.request_stop(run_id)
        background_tasks.add_task(service.schedule, "stop", run_id)
        return record

    @app.get("/api/runs/{run_id}/download")
    def download(run_id: str, request: Request):
        output_format = request.query_params.get("format", "json")
        if output_format not in {"json", "markdown"}:
            raise HTTPException(400, "Invalid download format.")
        record = service.refresh(run_id)
        extension = "json" if output_format == "json" else "md"
        content = _json_bytes(record) if output_format == "json" else _markdown(record)
        return Response(content, media_type="application/json" if output_format == "json" else "text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="workbench-{run_id}.{extension}"'})

    return app


def main(argv=None):
    import uvicorn

    parser = argparse.ArgumentParser(description="Local Azure TradingAgents workbench")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(port=args.port), host="127.0.0.1", port=args.port,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
