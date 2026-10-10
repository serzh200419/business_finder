"""Local commands use the same planner/providers/transactions as the API."""
import argparse
import json
import sys
import time
from pathlib import Path
from sqlalchemy import select
from app.db import SessionLocal
from app.models import BusinessSource, Campaign, Job
from app.planner import plan_campaign
from app.providers import CSVProvider
from app.schemas import CampaignInput
from app.services import (create_job, job_summary, resume_job, run_discovery, run_job, sync_plan)


def emit(label: str, value):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(label + ": " + json.dumps(value, ensure_ascii=False, default=str))


def fixture_demo():
    data = CampaignInput(niche="dentist", request_budget=100, candidate_budget=10,
        keywords={"hy": ["ատամնաբույժ"], "ru": ["стоматология"], "en": ["dentist", "permanent"]},
        provider_config={"discovery": "fixture", "fixture_scenario": "reliability", "page_size": 2,
                         "retry_base_seconds": 0.01})
    with SessionLocal() as db:
        campaign = Campaign(**data.model_dump())
        db.add(campaign)
        db.flush()
        sync_plan(db, campaign)
        db.commit()
        emit("SYNTHETIC FIXTURES ONLY — search plan", plan_campaign(data))
        job = create_job(db, campaign.id)
        job_id = job.id
        run_job(db, job_id)
        # Allow the persisted short retry deadline to pass; no external calls.
        time.sleep(0.03)
        job = run_job(db, job_id)
        emit("Partial failure", {"job_id": job.id, "status": job.status, **job_summary(db, job)})
        resume_job(db, job_id, retry_failed=True, disable_fixture_failures=True)
        job = run_job(db, job_id)
        emit("Resumed final summary", {"job_id": job.id, "status": job.status, **job_summary(db, job)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fixture-demo")
    commands.add_parser("assessment-demo")
    review_demo_parser = commands.add_parser("review-demo")
    review_demo_parser.add_argument("--output", type=Path, help="Write the synthetic UTF-8 BOM CSV")
    cleanup_parser = commands.add_parser("retention-cleanup")
    cleanup_parser.add_argument("--execute", action="store_true", help="Apply cleanup; otherwise dry-run")
    export_parser = commands.add_parser("export")
    export_parser.add_argument("campaign_id", type=int)
    export_parser.add_argument("--ids", type=int, nargs="+", required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    create = commands.add_parser("create")
    create.add_argument("file", type=Path)
    for name in ("plan", "discover", "csv-import", "jobs"):
        sub = commands.add_parser(name)
        sub.add_argument("campaign_id", type=int)
    validate = commands.add_parser("csv-validate")
    validate.add_argument("file", type=Path, help="Campaign JSON containing provider_config")
    resume = commands.add_parser("resume")
    resume.add_argument("job_id", type=int)
    resume.add_argument("--retry-failed", action="store_true")
    resume.add_argument("--disable-fixture-failures", action="store_true")
    inspect = commands.add_parser("job")
    inspect.add_argument("job_id", type=int)
    enrich = commands.add_parser("enrich")
    enrich.add_argument("campaign_id", type=int)
    enrich.add_argument("--options", type=Path, help="EnrichmentOptions JSON; defaults to synthetic empty search")
    enrich_resume = commands.add_parser("enrichment-resume")
    enrich_resume.add_argument("job_id", type=int)
    enrich_resume.add_argument("--request-budget", type=int)
    business = commands.add_parser("business")
    business.add_argument("business_id", type=int)
    args = parser.parse_args()
    if args.command == "fixture-demo":
        fixture_demo()
        return
    if args.command == "assessment-demo":
        assessment_demo()
        return
    with SessionLocal() as db:
        if args.command == "review-demo":
            from app.review_fixtures import review_demo
            emit("SYNTHETIC review demo", review_demo(db, args.output))
        elif args.command == "retention-cleanup":
            from app.retention import cleanup
            emit("Retention cleanup", cleanup(db, execute=args.execute))
        elif args.command == "export":
            from app.export import selected_export
            content, summary = selected_export(db, args.campaign_id, args.ids)
            args.output.write_bytes(content)
            emit("Selected export", summary)
        elif args.command == "enrich":
            from app.enrichment import create_enrichment_job, run_enrichment_job, enrichment_summary
            from app.schemas import EnrichmentOptions
            options = EnrichmentOptions.model_validate_json(args.options.read_text(encoding="utf-8-sig")) if args.options else EnrichmentOptions()
            job = create_enrichment_job(db, args.campaign_id, options)
            job = run_enrichment_job(db, job.id)
            emit("Enrichment", {"job_id": job.id, "status": job.status, **enrichment_summary(db, job)})
        elif args.command == "enrichment-resume":
            from app.enrichment import resume_enrichment, run_enrichment_job, enrichment_summary
            if args.request_budget is not None and not 0 <= args.request_budget <= 10000:
                parser.error("request budget must be between 0 and 10000")
            job = resume_enrichment(db, args.job_id, request_budget=args.request_budget)
            job = run_enrichment_job(db, job.id)
            emit("Enrichment", {"job_id": job.id, "status": job.status, **enrichment_summary(db, job)})
        elif args.command == "business":
            from app.enrichment_routes import business_details
            emit("Business", business_details(db, args.business_id))
        elif args.command == "create":
            data = CampaignInput.model_validate_json(args.file.read_text(encoding="utf-8-sig"))
            campaign = Campaign(**data.model_dump())
            db.add(campaign)
            db.flush()
            sync_plan(db, campaign)
            db.commit()
            emit("Created", {"campaign_id": campaign.id})
        elif args.command == "csv-validate":
            data = CampaignInput.model_validate_json(args.file.read_text(encoding="utf-8-sig"))
            rows, report = CSVProvider(data.provider_config).validate()
            existing = sum(db.scalar(select(BusinessSource.id).where(BusinessSource.provider == row.provider,
                BusinessSource.external_id == row.external_id)) is not None for row in rows)
            emit("CSV dry run", {**report, "existing": existing, "would_create": len(rows) - existing,
                                 "newly_created": 0, "dry_run": True})
        elif args.command in {"job", "resume"}:
            job = db.get(Job, args.job_id)
            if job is None:
                parser.error("Job not found")
            if args.command == "resume":
                resume_job(db, job.id, retry_failed=args.retry_failed,
                           disable_fixture_failures=args.disable_fixture_failures)
                job = run_job(db, job.id)
            emit("Job", {"job_id": job.id, "status": job.status, **job_summary(db, job)})
        else:
            campaign = db.get(Campaign, args.campaign_id)
            if campaign is None:
                parser.error("Campaign not found")
            if args.command == "plan":
                emit("Plan", plan_campaign(CampaignInput.model_validate(campaign, from_attributes=True)))
            elif args.command == "jobs":
                emit("Jobs", [{"job_id": j.id, "status": j.status, "result": job_summary(db, j)}
                              for j in db.scalars(select(Job).where(Job.campaign_id == campaign.id))])
            else:
                if args.command == "csv-import" and campaign.provider_config.get("discovery") != "csv":
                    parser.error("csv-import requires a CSV campaign")
                job = run_discovery(db, campaign.id)
                emit("Discovery", {"job_id": job.id, "status": job.status, **job.result})


def assessment_demo():
    from app.assessment_fixtures import seed_fixture_campaign
    from app.enrichment import create_enrichment_job, run_enrichment_job, effective_assessment
    from app.models import Contact, Evidence
    from sqlalchemy import func
    with SessionLocal() as db:
        campaign, options, expected = seed_fixture_campaign(db)
        job = create_enrichment_job(db, campaign.id, options)
        run_enrichment_job(db, job.id, max_steps=3)
        emit("SYNTHETIC checkpoint", {"campaign_id": campaign.id, "job_id": job.id, "status": job.status})
        job = run_enrichment_job(db, job.id)
        counts = lambda: (db.scalar(select(func.count()).select_from(Contact)), db.scalar(select(func.count()).select_from(Evidence)))
        before = counts()
        replay = create_enrichment_job(db, campaign.id, options)
        run_enrichment_job(db, replay.id)
        if counts() != before:
            raise RuntimeError("Fixture replay created duplicate contacts/evidence")
        outcomes = []
        for item in expected:
            assessment = effective_assessment(db, item["business_id"])
            if assessment.status != item["expected"]:
                raise RuntimeError(f"Fixture outcome mismatch: {item['name']}")
            outcomes.append({"business_id": item["business_id"], "name": item["name"], "status": assessment.status})
        emit("SYNTHETIC final outcomes", {"campaign_id": campaign.id, "job_id": job.id,
            "status": job.status, "requests_used": job.requests_used, "replay_duplicates": 0, "businesses": outcomes})


if __name__ == "__main__":
    main()
