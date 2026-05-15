# Cloud Backup Runbook

This project is Postgres-first. A safe cloud backup must include:

- a real Postgres dump
- runtime file archives for reports and local artifacts
- verification that the uploaded objects exist
- a restore test before deleting any local source data

## Command

```bash
cd ~/Investment_Tools
export BACKUP_BUCKET="your-gcs-bucket"
export BACKUP_PREFIX="investor-os-backups"
./bin/run_backup_cloud
```

Optional local archive cleanup after a successful upload:

```bash
./bin/run_backup_cloud --delete-local-archives
```

This only deletes the local backup artifacts created in `backups/cloud_backup_*`.
It does not delete your live local data.

## What gets backed up

- Postgres via `pg_dump --format=custom`
- runtime directories, if present:
  - `data`
  - `reports`
  - `outputs`
  - `filings`
  - `filing_docs`
  - `Notes`
  - `config`
  - `sql`

Excluded from the archive:

- `data/cache`
- `data/yf_cache`
- `logs`
- `frontend/node_modules`

## Restore test

Do not delete local source data until this passes.

1. Download the backup artifacts from GCS.
2. Restore Postgres into a non-production database:

```bash
pg_restore --clean --if-exists --no-owner --no-privileges \
  --dbname="postgresql://USER:PASSWORD@HOST:5432/RESTORE_DB" \
  postgres.dump
```

3. Extract the runtime archive into a separate restore directory:

```bash
mkdir -p /tmp/investor_restore_test
tar -xzf runtime_files.tar.gz -C /tmp/investor_restore_test
```

4. Point the app to the restored database and validate:
  - `GET /health/live`
  - `GET /health/ready`
  - open the main pages you care about

## Deletion policy

Safe to delete after verification:

- local backup archives in `backups/cloud_backup_*`
- older generated caches you intentionally do not want to retain locally

Not safe to delete until the restore test is complete and cloud retention is confirmed:

- the active local Postgres data source
- live runtime data under `data/`, `reports/`, `outputs/`, `filings/`, `filing_docs/`
