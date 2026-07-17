# Phase 1C PostgreSQL CI Validation

This runbook covers the disposable PostgreSQL 16 validation workflow for Slack
bot access-provisioning concurrency.

## Workflow

GitHub Actions workflow:

```text
.github/workflows/phase1c-postgres-concurrency.yml
```

Run it from GitHub with:

```text
Actions → Phase 1C PostgreSQL Concurrency → Run workflow
```

## Safety model

The workflow uses a GitHub Actions PostgreSQL 16 service container with:

- database name containing `test`
- test-only username
- test-only password scoped to the ephemeral workflow job
- no `DATABASE_URL`
- local service host only
- fake Slack and LLM configuration
- no production `.env`

The Django settings module is:

```text
config.settings.test_postgres
```

That settings module refuses to start if the PostgreSQL database name does not
contain `test`, if required `PHASE1C_POSTGRES_*` variables are missing, if the
host is not local/service-container scoped, or if `DATABASE_URL` is present.

## What the workflow proves

The workflow runs:

1. Django system checks.
2. Migration dry-run.
3. Full migrations against PostgreSQL.
4. Targeted compile/ruff checks for Phase 1 access code.
5. The three required PostgreSQL-only concurrency tests.
6. A skip guard that fails if any required concurrency test is skipped.
7. A 10-run flake loop for the same three tests.
8. Related Slack access-provisioning tests.
9. The full Slack bot test suite on PostgreSQL.
10. Evidence artifact upload.

The uploaded artifact is named:

```text
phase1c-postgres-test-evidence
```

## Required evidence to attach to Phase 1C

Download the artifact and keep:

- `phase1c-postgres-environment.txt`
- `phase1c-slack-bot-migrations.txt`
- `phase1c-concurrency-required.xml`
- `phase1c-concurrency-repeat-*.xml`
- `phase1c-related-slack-access.xml`
- `phase1c-full-slack-bot-postgres.xml`

Phase 1C should not be marked fully verified unless the workflow succeeds and
the required concurrency XML shows exactly three executed tests with zero
skipped tests.
