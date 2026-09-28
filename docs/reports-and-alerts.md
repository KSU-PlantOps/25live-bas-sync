[Documentation](README.md) › Reports and alerts

# Reports and alerts

- [The run report](#the-run-report)
- [Email](#email)
- [Webhooks](#webhooks)
- [Catch the job not running at all](#catch-the-job-not-running-at-all)

## The run report

Every live run builds a **report**:

- the outcome, the safety verdict, and each BAS system's status
- every warning and error it logged
- **every schedule with the exact windows written to it**, grouped by day:

```
[WRITTEN] Science Hall 1021  (webctrl:12100:5)
      Mon 10/05  07:45–10:30, 12:15–17:15
      Tue 10/06  07:45–09:30
[WRITTEN] Floor 2 of Liberal Arts  (webctrl:12200:120)
      no bookings — cleared
```

Each one is kept in `state/runs/`, and the web UI's **History** shows them,
with a CSV of every window.

Set `alerts.enabled: true` and it also goes out through either or both
channels below — on the web UI's **Settings → Alerts** page, or in
`config.yaml`. `--test-alert` (*Test alert* on the web UI) sends a sample
through each one.

Alerting never changes a run's outcome — a dead mail relay won't turn a
successful sync into a failure.

## Email

The full report as text and HTML, with every window attached as a CSV. Sent on
failure; set `email.notify_on_success: true` for a morning email of what the
buildings will do that week (`report: summary` leaves out the per-schedule
list).

- Transport: `starttls` (port 587, the default), `ssl` for implicit TLS/SMTPS
  (port 465), or `none` for an internal relay on a trusted network.
- Leave `username` blank for an open relay; if you set it, the password must
  be in `$BAS_SMTP_PASSWORD` (or set on the Alerts page).
- Connect, TLS, authentication and per-recipient rejection are each reported
  separately, because they need four different fixes.

## Webhooks

A short summary.

- `webhook_format: slack` (the default) posts `{"text": ...}`.
- `teams` posts the Adaptive Card that Microsoft Teams *Workflows* expects,
  since the old Office 365 connectors have been retired.
- `generic` adds `ok`, `exit_code` and `subject` for your own tooling.

It is sent on failure, and on success if `notify_on_success` (or
`webhook_notify_on_success`) is on. The URL is a credential — anyone holding it
can post to the channel — so it can come from `BAS_ALERT_WEBHOOK_URL` instead
of the file.

## Catch the job not running at all

Alerts only fire when the job runs. Set `monitoring.ping_url` to a
dead-man's-switch monitor — healthchecks.io, an Uptime Kuma push monitor,
Cronitor — and it's fetched after every successful run of the whole campus, so
the monitor alarms when the pings stop: an expired service password, a disabled
task, a rebuilt host. A run limited to one system or building doesn't ping it.
`ping_fail_url` is fetched after any failed run.

Pair it with a BAS-side heartbeat (`heartbeat_object` for BACnet,
`heartbeat_path` for the deprecated Niagara driver) and the BAS can alarm on
its own.
