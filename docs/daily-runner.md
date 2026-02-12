# Daily Runner

`mailtriage-daily` runs MailTriage in the background and handles workday-aware delivery.

## What it does

- Runs `mailtriage run --days 1` when today's report file is missing
- Writes a symlink: `<output.root>/latest.md` -> latest daily markdown report
- Uses `holidays` package for baseline country holidays (`country: US` by default)
- Applies manual overrides from local files
- Optionally ingests one or more ICS files as additional holidays
- Suppresses notification/open on weekend and holidays (configurable)
- Optional Jan 1 holiday-calendar download with VPN gate and reminder when missing
By default, it runs quietly and only surfaces Bitwarden unlock issues.

## Policy file

Create local policy from the redacted sample:

```bash
cp daily.policy.example.yml daily.policy.yml
```

`daily.policy.yml` is intended to stay local and is gitignored.

Edit `daily.policy.yml`:

- `env_file`: dotenv file used to load secret settings (default `.env`)
- `manual_holidays_file`: add custom holiday dates (one `YYYY-MM-DD` per line)
- `manual_workdays_file`: add exceptions that force a workday
- `ics_files`: add ICS files (for company holiday calendars)
- `holiday_download`: optional yearly refresh from URL or env var
- `notification`: control notification and auto-open behavior

For URL from env, set in `.env`:

```bash
ORG_HOLIDAYS_URL=https://your-internal-url/path/company.ics
```

Viewer settings in `.env`:

```bash
# Show only the most recent N days in the sidebar (0 = show all)
MAILTRIAGE_VIEW_DAYS=14
```

If Bitwarden is used for IMAP credentials, the daily runner will notify you when
Bitwarden is locked. You can also enable a modal dialog and auto-open the app
via `notification.bitwarden_locked` in your local policy file.

For background runs, the recommended approach is to use a Bitwarden CLI session token:

- Configure `bitwarden.session_file: .mailtriage/bw_session` in your policy
- When locked, the runner can show a copy/paste command (and copy it to clipboard) to populate that file
- Future runs load the token from that file into `BW_SESSION`

## Run manually

```bash
uv run mailtriage-daily --config config.yml --policy daily.policy.yml
```

Dry run preview:

```bash
uv run mailtriage-daily --config config.yml --policy daily.policy.yml --dry-run
```

## launchd setup (macOS)

Generate a plist from your local paths (daily, 7 days/week):

```bash
uv run mailtriage-launchd --repo . --config config.yml --policy daily.policy.yml --out /tmp/com.mailtriage.daily.plist
```

1. Copy plist:

```bash
cp /tmp/com.mailtriage.daily.plist ~/Library/LaunchAgents/com.mailtriage.daily.plist
```

2. Load it:

```bash
launchctl unload ~/Library/LaunchAgents/com.mailtriage.daily.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.mailtriage.daily.plist
```

3. Logs:

- `<output.root>/.mailtriage/logs/YYYY-MM-DD.out.log`
- `<output.root>/.mailtriage/logs/YYYY-MM-DD.err.log`

Logs are pruned automatically after 7 days.

The provided plist runs daily at `09:05` and also at login (`RunAtLoad`), which covers late starts.
