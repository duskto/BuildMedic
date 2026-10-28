import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import db, evaluation, evaluation_analysis, experiences, scoring, store
from .cases import CASES, metadata
from .clients import ModelClient, RunnerClient
from .config import settings
from .database import require_schema
from .errors import AppError, Blocked
from .github import GitHubClient
from .harness_support import capability
from .logs import LogWindow, log_window
from .profiles import match_profile
from .schemas import CreateTask, Envelope, Lifecycle, Preview, TaskPage, TaskSnapshot, ValidationRequest
from .security import read_owner, sanitize, sign_owner


@asynccontextmanager
async def lifespan(_app):
    require_schema()
    yield


app = FastAPI(title="BuildMedic API", version="0.2.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings().allowed_hosts.split(","))


def envelope(request, data):
    return {"data": data, "request_id": request.state.request_id}


def error_response(request, code, message, status, retryable=False, details=None):
    return JSONResponse(
        {
            "error": {"code": code, "message": message, "retryable": retryable, "details": details or {}},
            "request_id": getattr(request.state, "request_id", store.uid()),
        },
        status_code=status,
    )


@app.middleware("http")
async def session(request, call_next):
    request.state.request_id = store.uid()
    owner = read_owner(request.cookies.get("buildmedic_session"))
    request.state.owner = owner or secrets.token_hex(16)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return error_response(request, "origin_rejected", "请求来源不匹配", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return error_response(request, "origin_rejected", "不允许跨站写入", 403)
        length = request.headers.get("content-length", "0")
        if not length.isdecimal() or int(length) > 65536:
            return error_response(request, "request_too_large", "请求体超出限制", 413)
    response = await call_next(request)
    if not owner:
        response.set_cookie(
            "buildmedic_session",
            sign_owner(request.state.owner),
            httponly=True,
            secure=settings().cookie_secure,
            samesite="strict",
            max_age=30 * 86400,
        )
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    return error_response(request, "http_error", "接口不存在或请求方法不受支持", exc.status_code)


@app.exception_handler(Exception)
async def unexpected_error(request, _exc):
    return error_response(request, "platform_error", "服务暂时不可用，请保留 request_id 后重试", 500, True)


@app.exception_handler(AppError)
async def app_error(request, exc):
    return error_response(request, exc.code, exc.message, exc.status, exc.retryable, exc.details)


@app.exception_handler(store.Conflict)
async def conflict(request, exc):
    return error_response(request, "conflict", str(exc), 409)


@app.exception_handler(Blocked)
def environment_unavailable(request, exc):
    return error_response(request, "environment_unavailable", str(exc), 503)


@app.exception_handler(KeyError)
async def not_found(request, _exc):
    return error_response(request, "not_found", "任务或产物不存在", 404)


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    # Do not echo user-supplied data or secrets in validation errors.
    return error_response(
        request,
        "invalid_request",
        "请求字段不符合接口要求",
        422,
        details={"fields": [".".join(map(str, e["loc"])) for e in exc.errors()]},
    )


@app.get("/api/v1/health")
async def health(request: Request):
    runner = RunnerClient()
    try:
        value = await runner.health()
    except Exception as exc:  # noqa: BLE001 - Health endpoint must report unavailable for any Runner failure.
        value = {"ready": False, "message": sanitize(str(exc)), "backend": None}
    finally:
        await runner.close()
    return envelope(
        request,
        {
            "model_ready": settings().model_ready,
            "model_id": settings().model_id or None,
            "runner": value,
            "adaptive_ready": capability()["ready"],
            "adaptive_message": capability()["message"],
            "agent_tools_ready": settings().agent_tools_ready,
            "max_model_calls": settings().max_model_calls,
            "scoring_ready": settings().scoring_ready,
        },
    )


@app.post("/api/v1/model/check")
async def check_model(request: Request):
    model = ModelClient()
    try:
        return envelope(request, await model.probe())
    except Blocked as exc:
        raise AppError("model_unconfigured", str(exc), 422) from exc
    finally:
        await model.close()


@app.get("/api/v1/demo-cases")
def demo_cases(request: Request):
    return envelope(
        request,
        [
            {
                **case,
                **metadata(case_id),
                "id": case_id,
                "framework": "pytest",
                "environment_status": "blocked" if case.get("blocked") else "requires_runner",
            }
            for case_id, case in CASES.items()
        ],
    )


@app.get("/api/v1/stats")
def task_stats(request: Request):
    from sqlalchemy import select

    with db.transaction() as conn:
        rows = (
            conn.execute(select(db.tasks.c.snapshot).where(db.tasks.c.owner_id == request.state.owner))
            .scalars()
            .all()
        )
    return envelope(
        request,
        {
            "total": len(rows),
            "active": sum(r["lifecycle"] in {"queued", "running", "cancelling"} for r in rows),
            "repaired": sum(r["outcome"] == "repaired" for r in rows),
            "blocked": sum(r["outcome"] == "blocked" for r in rows),
        },
    )


@app.post("/api/v1/ci/preview")
async def preview_ci(request: Request, body: Preview):
    github = GitHubClient()
    try:
        return envelope(request, await github.preview(body.run_url))
    finally:
        await github.close()


async def source_metadata(body):
    if body.source.type == "demo":
        if body.source.case_id not in CASES:
            raise AppError("unknown_case", "演示案例不存在", 422)
        return metadata(body.source.case_id)
    github = GitHubClient()
    try:
        preview = await github.preview(body.source.run_url, body.source.run_attempt)
    finally:
        await github.close()
    selected = next((job for job in preview["jobs"] if job["id"] == body.source.job_id), None)
    if not selected:
        raise AppError("invalid_job", "所选失败 Job 不属于固定 Run attempt", 422)
    profile = match_profile(
        preview["repo"],
        preview["head_sha"],
        preview.get("workflow_path"),
        selected["name"],
        preview.get("head_repository"),
    )
    return {key: preview[key] for key in ("repo", "run_id", "run_attempt", "head_sha", "profile_version")} | {
        "job_id": selected["id"],
        "title": selected["name"],
        "source_type": "github_actions",
        "case_id": None,
        "execution_label": "固定 GitHub SHA · 登记环境本地验证"
        if profile
        else "GitHub 失败日志诊断 · 尚未登记执行环境",
        "runner_profile": profile.binding() if profile else None,
        "profile_version": profile.id if profile else None,
        "snapshot_sha256": profile.snapshot_sha256 if profile else None,
        "workflow_path": profile.workflow_path if profile else preview.get("workflow_path"),
    }


def require_key(key):
    if not key or not 8 <= len(key) <= 128:
        raise AppError("idempotency_key_required", "请提供 8–128 字符的 Idempotency-Key", 422)


def check_mode(body):
    if body.limits.max_model_calls > settings().max_model_calls:
        raise AppError("platform_budget_exceeded", "请求调用预算超过当前服务端上限", 422)
    if body.agent_tools:
        if not settings().agent_tools_ready:
            raise AppError(
                "agent_tools_unavailable",
                "工具诊断需配置兼容 Function Calling 的模型，DeepSeek 需关闭思考模式",
                422,
            )
        minimum = (12 if body.experience else 11) if body.mode == "adaptive" else 4
        if body.limits.max_model_calls < minimum or body.limits.max_tokens < 24000:
            raise AppError(
                "agent_budget_too_small", f"当前工具模式至少需要 {minimum} 次调用和 24,000 Token 预算", 422
            )
    if body.mode != "adaptive":
        return
    status = capability()
    if not status["ready"]:
        raise AppError("adaptive_unavailable", status["message"], 422)
    calls = 8 if body.experience else 7
    recommended = "150,000（含经验反思）" if body.experience else "100,000"
    if body.limits.max_model_calls < calls or body.limits.max_tokens < 48000:
        raise AppError(
            "adaptive_budget_too_small",
            f"当前 Harness² 模式至少需要 {calls} 次模型调用和 48,000 Token 预算，推荐 {recommended}",
            422,
        )


@app.post("/api/v1/tasks", status_code=202)
async def create_task(request: Request, body: CreateTask, idempotency_key: str = Header(default="")):
    require_key(idempotency_key)
    tid = store.existing_request(request.state.owner, idempotency_key, body.model_dump())
    if tid is None:
        check_mode(body)
        meta = await source_metadata(body)
        if body.agent_tools and body.source.type == "github_actions" and not meta.get("runner_profile"):
            raise AppError("agent_tools_unavailable", "所选 Job 仅支持日志诊断，尚未登记工具执行环境", 422)
        if (
            body.mode == "adaptive"
            and body.source.type == "github_actions"
            and not meta.get("runner_profile")
        ):
            raise AppError("adaptive_source_unsupported", "所选 Job 的仓库、SHA 和复现环境尚未登记", 422)
        tid = store.create(request.state.owner, idempotency_key, body.model_dump(), meta)
    return envelope(
        request,
        {"task_id": tid, "lifecycle": store.snapshot(tid)["lifecycle"], "detail_url": f"/tasks/{tid}"},
    )


@app.get("/api/v1/tasks", response_model=Envelope[TaskPage])
def list_tasks(
    request: Request,
    lifecycle: Lifecycle | None = None,
    repo: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return envelope(request, store.list_tasks(request.state.owner, lifecycle, repo, page, page_size))


@app.post("/api/v1/experiments", status_code=201)
def create_experiment(
    request: Request, body: evaluation.ExperimentRequest, idempotency_key: str = Header(default="")
):
    require_key(idempotency_key)
    # Creation is a local preregistration. Actual model/Runner readiness is checked on execution.
    eid = evaluation.create(request.state.owner, idempotency_key, body)
    return envelope(request, evaluation.get(eid, request.state.owner))


@app.get("/api/v1/experiments")
def list_experiments(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50)):
    return envelope(request, evaluation.listing(request.state.owner, page, page_size))


@app.get("/api/v1/evaluation/cases")
def evaluation_cases(request: Request):
    return envelope(request, evaluation.available_cases())


@app.get("/api/v1/experiments/{eid}")
def get_experiment(eid: str, request: Request):
    return envelope(request, evaluation.get(eid, request.state.owner))


@app.post("/api/v1/experiments/{eid}/advance")
def advance_experiment(eid: str, request: Request):
    return envelope(request, evaluation.advance(eid, request.state.owner, check_task=check_mode))


@app.post("/api/v1/experiments/{eid}/cancel")
def cancel_experiment(eid: str, request: Request):
    evaluation.cancel(eid, request.state.owner)
    return envelope(request, evaluation.get(eid, request.state.owner))


@app.get("/api/v1/experiments/{eid}/report")
def experiment_report(eid: str, request: Request):
    report = evaluation.export_report(eid, request.state.owner)
    return Response(
        json.dumps(report, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="experiment-{report["id"]}.json"',
            "Cache-Control": "private, no-store",
        },
    )


@app.get(
    "/api/v1/experiments/{eid}/analysis", response_model=Envelope[evaluation_analysis.EvaluationAnalysis]
)
def experiment_analysis(eid: str, request: Request):
    return envelope(request, evaluation_analysis.analyze(evaluation.get(eid, request.state.owner)))


@app.get("/api/v1/experiments/{eid}/analysis/report")
def experiment_analysis_report(eid: str, request: Request):
    report = evaluation_analysis.export_analysis(eid, request.state.owner)
    return Response(
        json.dumps(report, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="analysis-{eid}.json"',
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/api/v1/experiments/{eid}/score", status_code=202)
async def start_experiment_score(eid: str, request: Request):
    if await request.body():
        raise AppError("scoring_body_forbidden", "评分只使用服务端冻结材料，不接收客户端指标或私有路径", 422)
    scoring.reserve(eid, request.state.owner)
    return envelope(request, await scoring.get(eid, request.state.owner))


@app.get("/api/v1/experiments/{eid}/score")
async def get_experiment_score(eid: str, request: Request):
    return envelope(request, await scoring.get(eid, request.state.owner))


@app.get("/api/v1/experiments/{eid}/score/report")
async def experiment_score_report(eid: str, request: Request):
    result = await scoring.get(eid, request.state.owner)
    if not result:
        raise KeyError(eid)
    return Response(
        json.dumps(result, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="score-{result["id"]}.json"',
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/v1/tasks/{tid}", response_model=Envelope[TaskSnapshot])
def get_task(tid: str, request: Request):
    return envelope(request, store.snapshot(tid, request.state.owner))


@app.post("/api/v1/tasks/{tid}/cancel", response_model=Envelope[TaskSnapshot])
def cancel_task(tid: str, request: Request):
    return envelope(request, store.cancel(tid, request.state.owner))


@app.post("/api/v1/tasks/{tid}/retry", status_code=202)
async def retry_task(tid: str, request: Request, idempotency_key: str = Header(default="")):
    require_key(idempotency_key)
    original = store.snapshot(tid, request.state.owner)
    if original.get("experiment_binding", {}).get("protocol") == "four-arm-v1":
        raise AppError(
            "experiment_retry_unsupported", "实验任务需通过新清单重复运行，不能改变已登记的组别与次数", 422
        )
    body = CreateTask.model_validate(store.task_body(tid, request.state.owner))
    new_id = store.existing_request(request.state.owner, idempotency_key, body.model_dump(), parent_id=tid)
    if new_id is None:
        check_mode(body)
        meta = await source_metadata(body)
        if body.agent_tools and body.source.type == "github_actions" and not meta.get("runner_profile"):
            raise AppError("agent_tools_unavailable", "所选 Job 尚未登记工具执行环境", 422)
        if (
            body.mode == "adaptive"
            and body.source.type == "github_actions"
            and not meta.get("runner_profile")
        ):
            raise AppError("adaptive_source_unsupported", "所选 Job 的复现环境尚未登记", 422)
        parent = store.snapshot(tid, request.state.owner)
        if parent.get("runner_profile") and meta.get("runner_profile") != parent["runner_profile"]:
            raise AppError("profile_changed", "原任务的登记 profile 已变化，请创建新的诊断", 422)
        new_id = store.create(request.state.owner, idempotency_key, body.model_dump(), meta, parent_id=tid)
    return envelope(request, {"task_id": new_id, "detail_url": f"/tasks/{new_id}"})


@app.get("/api/v1/tasks/{tid}/candidates")
def get_candidates(tid: str, request: Request):
    return envelope(request, store.snapshot(tid, request.state.owner)["candidates"])


@app.post("/api/v1/tasks/{tid}/experience")
def publish_experience(tid: str, request: Request, idempotency_key: str = Header(default="")):
    require_key(idempotency_key)
    return envelope(request, experiences.publish(tid, request.state.owner, idempotency_key))


@app.get("/api/v1/experience")
def list_experience(
    request: Request,
    domain: str | None = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return envelope(request, experiences.list_versions(request.state.owner, domain, page, page_size))


@app.get("/api/v1/experience/snapshots/{sid}")
def get_experience_snapshot(sid: str, request: Request):
    return envelope(request, experiences.get_snapshot(sid, request.state.owner))


@app.get("/api/v1/experience/{vid}")
def get_experience(vid: str, request: Request):
    return envelope(request, experiences.get_version(vid, request.state.owner))


@app.post("/api/v1/experience/{vid}/revoke")
def revoke_experience(vid: str, request: Request):
    return envelope(request, experiences.revoke(vid, request.state.owner))


@app.get("/api/v1/tasks/{tid}/validations")
def get_validations(tid: str, request: Request):
    return envelope(request, store.snapshot(tid, request.state.owner)["validations"])


@app.post("/api/v1/tasks/{tid}/validations", status_code=202)
def revalidate(tid: str, request: Request, body: ValidationRequest):
    vid = store.enqueue_validation(tid, request.state.owner, body.candidate_id)
    return envelope(request, {"validation_id": vid, "status": "queued"})


@app.get("/api/v1/tasks/{tid}/artifacts/{aid}/lines", response_model=Envelope[LogWindow])
def get_log_lines(
    tid: str,
    aid: str,
    request: Request,
    start_line: int = Query(1, ge=1),
    start_column: int = Query(0, ge=0, le=2000000),
    max_lines: int = Query(100, ge=1, le=200),
):
    artifact = store.get_artifact(tid, aid, request.state.owner)
    response = envelope(request, log_window(artifact, start_line, start_column, max_lines).model_dump())
    return JSONResponse(response, headers={"Cache-Control": "private, no-store"})


@app.get("/api/v1/tasks/{tid}/artifacts/{aid}")
def get_artifact(tid: str, aid: str, request: Request, download: bool = False):
    artifact = store.get_artifact(tid, aid, request.state.owner)
    suffix = (
        "patch"
        if artifact["kind"] == "patch"
        else "json"
        if artifact["content_type"].startswith("application/json")
        else "txt"
    )
    headers = {"ETag": '"' + artifact["sha256"] + '"', "Cache-Control": "private, no-store"}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="buildmedic-{artifact["kind"]}-{aid[:8]}.{suffix}"'
        )
    return Response(artifact["content"], media_type=artifact["content_type"], headers=headers)


@app.get("/api/v1/tasks/{tid}/events")
async def task_events(
    tid: str, request: Request, after: int = Query(0, ge=0), last_event_id: str | None = Header(None)
):
    snap = store.snapshot(tid, request.state.owner)
    if last_event_id is not None:
        if not last_event_id.isdecimal():
            raise AppError("invalid_event_cursor", "Last-Event-ID 必须是非负整数", 422)
        after = int(last_event_id)

    async def stream():
        cursor = after
        if cursor > snap["last_event_seq"]:
            yield 'event: stream.reset\ndata: {"reason":"cursor_ahead"}\n\n'
            return
        ping = time.monotonic()
        while not await request.is_disconnected():
            rows, terminal, last = await asyncio.to_thread(
                store.read_events, tid, cursor, request.state.owner
            )
            for event in rows:
                cursor = event["seq"]
                yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
            if terminal and cursor >= last:
                return
            if time.monotonic() - ping >= 15:
                yield ": ping\n\n"
                ping = time.monotonic()
            await asyncio.sleep(0.25)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Production build and API share an origin; dev uses Vite's /api proxy.
dist = settings().web_dist
if (dist / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def web(path: str, request: Request):
    if path.startswith("api/"):
        raise AppError("not_found", "API 不存在", 404)
    index = dist / "index.html"
    if not index.is_file():
        return JSONResponse(
            {
                "message": "Frontend build unavailable. Run npm install && npm run build in apps/web.",
                "api_docs": "/docs",
            },
            status_code=503,
        )
    return FileResponse(index)
