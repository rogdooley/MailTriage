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

## Run manually

```bash
uv run mailtriage-daily --config config.yml --policy daily.policy.yml
```

Dry run preview:

```bash
uv run mailtriage-daily --config config.yml --policy daily.policy.yml --dry-run
```

## launchd setup (macOS)

1. Copy plist:

```bash
cp /Users/dooley/Documents/GithubClone/MailTriage/scripts/com.dooley.mailtriage.daily.plist ~/Library/LaunchAgents/
```

2. Load it:

```bash
launchctl unload ~/Library/LaunchAgents/com.dooley.mailtriage.daily.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.dooley.mailtriage.daily.plist
```

3. Logs:

- `/tmp/mailtriage-daily.out.log`
- `/tmp/mailtriage-daily.err.log`

The provided plist runs daily at `09:05` and also at login (`RunAtLoad`), which covers late starts.
