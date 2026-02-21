# Backup Policy - Investor OS v2

## 1. Objectives
- Prevent data loss (DB + reports + app state)
- Fast restore to a known-good point
- Minimize local disk usage growth

## 2. Scope
Back up:
- Source code (`app/`, `tools/`, `automations/`, `bin/`)
- Runtime data (`data/`, `reports/`, `onyx_brain.db`)
- Config templates (`.env.example`, docs)

Exclude from frequent backups:
- old backup archives (`backups/*`)
- `.git/*`
- temporary caches where recoverable

## 3. Current Full Backup
Latest full archive created:
- `backups/investment_tools_backup_latest.tar`

## 4. Recommended Strategy
Use 3-tier policy:
1. Weekly full backup
2. Daily incremental snapshot backup
3. Monthly restore test

## 5. Practical Option (Recommended)
Use `restic` for encrypted incremental snapshots.
If not available, use `rsync --link-dest` snapshots on local/external disk.

## 6. Manual Full Backup Command
```bash
cd /Users/solmaz/Investment_Tools
tar -cf backups/investment_tools_backup_$(date +%Y%m%d_%H%M%S).tar \
  --exclude='./backups/*' --exclude='./.git/*' .
```

## 7. Verification Checklist
After backup creation:
```bash
ls -lh backups/investment_tools_backup_*.tar | tail -n 1
tar -tf backups/investment_tools_backup_latest.tar | head
```

After cloud upload:
- Confirm upload completed
- Confirm cloud file size matches local size

## 8. Retention Policy
Local rolling retention:
- Keep backups for the last 7 days only.
- Auto-delete anything older than 7 days on each backup run.

Run:
```bash
cd /Users/solmaz/Investment_Tools
./bin/run_backup_local
```

Dry run retention only:
```bash
cd /Users/solmaz/Investment_Tools
./bin/run_backup_local --retention-only --dry-run
```

## 9. Restore Procedure (Tar)
1. Create clean restore folder.
2. Extract full backup.
3. Validate key files and DB presence.

Example:
```bash
mkdir -p /tmp/investor_restore_test
cd /tmp/investor_restore_test
tar -xf /Users/solmaz/Investment_Tools/backups/investment_tools_backup_latest.tar
```

## 10. Safety Rules
- Never delete old backups before verifying new backup integrity.
- Keep one extra known-good backup in cloud.
- Run periodic restore tests, not just backup generation.

---
Last updated: 2026-02-17
