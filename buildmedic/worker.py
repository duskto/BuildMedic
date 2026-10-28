"""Database worker for local development; Celery dispatcher for Redis deployments."""

import argparse
import asyncio
import copy
import logging
import signal
import time

from celery import Celery
from sqlalchemy import select, update

from . import db, store
from .clients import RunnerClient
from .config import settings
from .database import require_schema
from .orchestration import run_claimed

log = logging.getLogger(__name__)


def run_async(coroutine):
    try:
        import uvloop
    except ImportError:
        return asyncio.run(coroutine)
    return asyncio.run(coroutine, loop_factory=uvloop.new_event_loop)


celery_app = Celery("buildmedic", broker=settings().redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    task_track_started=False,
)


def pending_jobs():
    with db.transaction() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(db.outbox)
                .where(db.outbox.c.published_at.is_(None))
                .order_by(db.outbox.c.created_at)
                .limit(20)
            ).mappings()
        ]


def get_job(job_id):
    with db.transaction() as conn:
        value = conn.execute(select(db.outbox).where(db.outbox.c.id == job_id)).mappings().first()
        return dict(value) if value else None


def mark_published(job_id):
    with db.transaction() as conn:
        conn.execute(update(db.outbox).where(db.outbox.c.id == job_id).values(published_at=db.now()))


def execute_job(job_id):
    job = get_job(job_id)
    if not job:
        return
    claimed = store.claim(job["task_id"], job["payload"].get("validation_id"))
    mark_published(job_id)
    if not claimed:
        return
    run_async(
        run_claimed(
            job["task_id"], claimed, validation_job=job["payload"] if job["kind"] == "validate" else None
        )
    )


@celery_app.task(name="buildmedic.execute")
def queued_execute(job_id):
    require_schema()
    execute_job(job_id)


async def reap_expired():
    with db.transaction() as conn:
        expired = list(
            conn.execute(
                select(db.tasks)
                .where(
                    db.tasks.c.lease_token.is_not(None),
                    db.tasks.c.heartbeat_at < db.now() - settings().lease_seconds,
                )
                .with_for_update()
            ).mappings()
        )
        victims = []
        for task in expired:
            token = store.uid()
            snap = copy.deepcopy(task["snapshot"])
            store.persist(
                conn,
                task,
                snap,
                "worker.lost",
                {"message": "Worker 心跳过期，正在清理旧执行"},
                lease_token=token,
                heartbeat_at=db.now(),
            )
            victims.append((task["id"], token, snap))
    if not victims:
        return
    runner = RunnerClient()
    try:
        for task_id, token, snap in victims:
            cleanup_ok = True
            for execution in snap["executions"]:
                if execution.get("status", execution.get("lifecycle")) in {"queued", "running"}:
                    try:
                        cleanup_ok = await runner.cancel(execution["id"]) and cleanup_ok
                    except Exception:  # noqa: BLE001 - Retain durable outbox or cleanup state on transport failure.
                        cleanup_ok = False
            message = (
                "Worker 心跳丢失，旧执行已清理；请创建关联重试"
                if cleanup_ok
                else "Worker 心跳丢失，无法确认旧执行已清理"
            )
            pending = [
                v
                for v in snap["validations"]
                if v.get("kind") == "revalidation" and v["status"] in {"queued", "running"}
            ]
            if snap["lifecycle"] in {"completed", "failed", "cancelled", "interrupted"} and pending:
                for validation in pending:
                    store.save_validation(
                        task_id,
                        token,
                        dict(validation, status="interrupted", reasons=[message]),
                        revalidation=True,
                    )
            else:
                store.finish(task_id, token, "interrupted", message=message)
    finally:
        await runner.close()


def main():
    parser = argparse.ArgumentParser(description="BuildMedic durable worker")
    parser.add_argument(
        "--dispatch", action="store_true", help="publish outbox jobs to Celery/Redis instead of running them"
    )
    parser.add_argument("--once", action="store_true", help="process one available batch and exit")
    args = parser.parse_args()
    require_schema()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        run_async(reap_expired())
        jobs = pending_jobs()
        for job in jobs:
            if stopping:
                break
            if args.dispatch or settings().queue_backend == "celery":
                try:
                    queued_execute.apply_async(args=[job["id"]], task_id=job["id"])
                    mark_published(job["id"])
                except Exception:  # noqa: BLE001 - Retain durable outbox or cleanup state on transport failure.
                    log.warning("Queue unavailable; outbox retained for retry")
                    break
            else:
                execute_job(job["id"])
        if args.once:
            break
        time.sleep(0.5)


if __name__ == "__main__":
    main()
