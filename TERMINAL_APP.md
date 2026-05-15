# Investor Terminal App

`Investor Terminal App` is the local software layer on top of your existing terminal workflows.

## Start

```bash
./bin/run_terminal_app
```

Default URL:

`http://127.0.0.1:8765`

Optional custom host/port:

```bash
./bin/run_terminal_app 127.0.0.1 8788
```

## What it gives you

- Browser dashboard with status + alerts + earnings.
- Buttons to run:
  - Daily
  - Weekly
  - Monthly
  - All
  - Build Beta dashboard
- Job state tracking and per-job logs in `logs/manual_webapp_*.log`.

## Notes

- Your existing `./bin/investor` terminal UI remains unchanged.
- If external data providers are unreachable, jobs can fail; the app still stays available for navigation and logs.
