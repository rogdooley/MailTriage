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

## Bitwarden Unlock (Keychain/Secret Store)

For unattended background runs without keeping the vault unlocked, the launchd wrapper can:

1. Read your Bitwarden master password from an OS secret store
2. Run `bw unlock --passwordenv BW_PASSWORD --raw` to get a per-run session token
3. Run MailTriage
4. Immediately `bw lock`

This avoids a plaintext password file.

### macOS (Keychain)

Store or update the master password in Keychain (service is `mailtriage/bitwarden`, account is your macOS username):

```bash
security add-generic-password -U -s "mailtriage/bitwarden" -a "$USER" -w
```

Verify you can read it:

```bash
security find-generic-password -w -s "mailtriage/bitwarden" -a "$USER" >/dev/null && echo OK
```

### Linux (Secret Service)

Store (you'll be prompted):

```bash
secret-tool store --label="MailTriage Bitwarden" service mailtriage/bitwarden user "$USER"
```

Verify:

```bash
secret-tool lookup service mailtriage/bitwarden user "$USER" >/dev/null && echo OK
```

### Override Keys (Optional)

The launch wrapper supports:

- `MAILTRIAGE_BW_STORE_SERVICE` (default `mailtriage/bitwarden`)
- `MAILTRIAGE_BW_STORE_USER` (default `$USER`)

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

## Hourly Watch Job (macOS)

If you enable `watch.unreplied`, run an hourly watcher to avoid missing messages during the day:

1. Copy the plist and edit the `EnvironmentVariables` section:

```bash
cp scripts/com.mailtriage.watch.hourly.plist ~/Library/LaunchAgents/
```

2. Load it:

```bash
launchctl unload ~/Library/LaunchAgents/com.mailtriage.watch.hourly.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.mailtriage.watch.hourly.plist
```

3. Logs:

- `<output.root>/.mailtriage/logs/watch-YYYY-MM-DD.out.log`
- `<output.root>/.mailtriage/logs/watch-YYYY-MM-DD.err.log`

Notifications:

- macOS notifications use `terminal-notifier` if installed (`brew install terminal-notifier`).
- When an unreplied-SLA rule triggers, the notification opens `<output.root>/watch/unreplied.html` on click.
