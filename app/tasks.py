from celery import Celery
from app.config import get_settings
from app.db import SessionLocal
from app.models import Job, JobQuery, JobBusiness, Campaign, now
from app.services import LeaseBusy, LeaseLost, create_job, run_job, utc
from sqlalchemy import select

celery_app = Celery("business_finder", broker=get_settings().redis_url)
celery_app.conf.update(task_serializer="json", accept_content=["json"],
                       task_acks_late=True, task_reject_on_worker_lost=True,
                       worker_prefetch_multiplier=1)


@celery_app.task
def discover_campaign(campaign_id: int) -> dict:
    with SessionLocal() as session:
        job = create_job(session, campaign_id)
        job_id = job.id
    discover_job.delay(job_id)
    return {"job_id": job_id}


@celery_app.task(bind=True, max_retries=None)
def discover_job(self, job_id: int) -> dict:
    with SessionLocal() as session:
        try:
            job = run_job(session, job_id)
        except (LeaseBusy, LeaseLost) as exc:
            # Duplicate delivery cannot process the campaign; retry after lease recovery.
            raise self.retry(exc=exc, countdown=get_settings().discovery_lease_seconds + 1)
        progress_model = JobBusiness if job.kind == "enrichment" else JobQuery
        pending = list(session.scalars(select(progress_model).where(progress_model.job_id == job.id,
                                                              progress_model.status == "pending")))
        if pending and job.status == "partial" and job.result.get("stop_reason") not in {
                "request_budget", "candidate_budget", "interrupted; inspect worker logs"}:
            due = min(utc(q.next_attempt_at) if q.next_attempt_at else now() for q in pending)
            countdown = max(0.01, (due - now()).total_seconds())
            raise self.retry(countdown=countdown)
        return {"job_id": job.id, "status": job.status}
