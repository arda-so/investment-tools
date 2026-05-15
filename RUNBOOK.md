# Runbook - Investor OS v2

## 1. Service Start/Stop
Start app:
```bash
cd /Users/solmaz/Investment_Tools
source .venv-memory/bin/activate
./bin/run_v2_app
```

If port is stuck:
```bash
lsof -ti tcp:8766 | xargs kill -9
```

## 2. Health Checks
Basic route checks:
```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/dashboard
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/reports
curl -sS -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8766/company_file?t=ADBE'
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/organizer
```

Expected: `200`.

## 3. Incident: Internal Server Error
1. Reproduce with `curl` route.
2. Run app in foreground and capture traceback.
3. Fix route/template mismatch first (most common cause).
4. Re-run smoke tests.

Foreground run:
```bash
source .venv-memory/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8766
```

## 4. Incident: Missing Live Data
Potential causes:
- API key missing/expired
- upstream timeout
- cache fallback showing stale snapshot

Actions:
1. Check `.env` keys.
2. Verify network/API reachability.
3. Trigger refresh route or restart app.

## 5. Incident: Google Features Unavailable
Symptoms: memory/google panels show unavailable.

Actions:
1. Ensure Google dependencies installed.
2. Verify OAuth client secret path.
3. Reconnect from `/organizer/google/connect`.

## 6. Database Recovery
Primary data lives in `data/*.db` and `onyx_brain.db`.
Before risky changes, copy DBs:
```bash
cp data/core.db data/core.db.bak.$(date +%Y%m%d_%H%M%S)
cp onyx_brain.db onyx_brain.db.bak.$(date +%Y%m%d_%H%M%S)
```

## 7. Deploy/Release Sanity Checklist
- Dashboard loads
- Reports loads
- Company detail loads
- Organizer loads
- Portfolio loads
- No traceback in app logs

## 8. Automation Jobs
Scripts under:
- `automations/*.sh`
- `automations/launchd/*.plist`

Verify launchd registration if needed:
```bash
launchctl list | rg 'com.solmaz'
```

## 9. Backup/Restore Pointer
See `BACKUP_POLICY.md` for policy and exact restore sequence.

---
Last updated: 2026-02-17
