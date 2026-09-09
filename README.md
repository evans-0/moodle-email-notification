# Moodle Assignment Notifier

Get emailed when your Moodle site posts a new assignment or changes a due date —
even if Moodle's own notification system isn't reaching you (a common problem
that's often just a setting, but sometimes just doesn't work).

Runs entirely for free: a GitHub Actions workflow polls your personal Moodle
calendar feed every 30 minutes, compares it against what it's already told you
about, and emails you a summary of anything new — including the course name
and due date. No server to maintain, no laptop that has to stay on.

## How it works

```
 cron-job.org (free, fires every 30 min on its own precise clock)
        |
        v
 GitHub Actions workflow  <-- can also be
        |                     triggered manually any time from the Actions tab
        v
 Fetches your Moodle calendar export (iCal) feed
        |
        v
 Compares event UIDs against seen_events.json (committed back to this repo
 after every run, so state survives between runs)
        |
        v
 New events found?  -->  Email via Gmail SMTP, with title / course / due date
 Nothing new?        -->  Exit quietly, no email
```

### Why cron-job.org instead of just GitHub's schedule?

GitHub Actions' own `schedule:` trigger is "best effort" — GitHub explicitly
delays or skips scheduled runs under load, and this gets much worse for
schedules more frequent than about an hour. In practice, a `*/30 * * * *`
GitHub schedule can end up firing every 2-3 hours instead. cron-job.org calls
GitHub's API on its own clock to fire the workflow on demand
(`repository_dispatch`), sidestepping GitHub's scheduler entirely — this repo
relies on cron-job.org alone for scheduling (no native GitHub schedule), so
if cron-job.org ever has an outage, checks pause until it's back. Turning on
cron-job.org's "notify me if this job gets disabled" option (see step 6) is
worth doing so you'd actually notice if that happened.

## Setup

This takes about 15-20 minutes the first time. None of your secrets (Moodle
calendar token, Gmail password) are ever committed to this repo — they're
stored as encrypted GitHub Actions secrets, and the cron-job.org config lives
only in your own cron-job.org account.

### 1. Get your Moodle calendar export URL

1. Log into your Moodle site.
2. Click your profile picture (top right) -> **Preferences**.
3. Under **Calendar**, click **Export calendar**.
4. Set "Events to export" to **All events**.
5. Set "Time period" to **Custom range** (defaults to about a year ahead —
   this matters, because the "Recent and next 60 days" option is a *rolling*
   window and won't show assignments due further out until they get close,
   which defeats the point of getting notified early).
6. Click **Get calendar URL** and copy the URL it generates. It'll contain
   `authtoken=` in it — **treat this like a password**. Anyone with this URL
   can see your calendar.

### 2. Get your own copy

**If you're fine with your fork being public:** just click **Fork** on
GitHub. Simplest option. Note that GitHub forks of a public repo are always
public too (there's no "private fork" option in the standard UI) — this
matters because once your workflow runs, it commits your own
`seen_events.json` back to your fork, which would then be publicly visible.
That file only ever contains opaque numeric event IDs and a timestamp (no
course names, titles, due dates, or content), so the actual exposure is
minor, but it's still *your* data going public by default.

**If you'd rather nothing be public:** don't use the Fork button. Instead,
clone this repo and push it to a brand-new private repo of your own:

```
git clone https://github.com/<original-owner>/<this-repo>.git my-moodle-notifier
cd my-moodle-notifier
git remote remove origin
git remote add origin https://github.com/<your-username>/my-moodle-notifier.git
git push -u origin main
```

(Create the empty private repo on GitHub first, without initializing it with
any files, then run the commands above.) Either way, none of your secrets
(calendar token, Gmail password) are ever committed regardless of visibility
— those live only in encrypted repo secrets.

### 3. Generate a Gmail App Password

This lets the workflow send email through your Gmail account without giving
it your actual password.

1. Your Google account needs **2-Step Verification** turned on first
   (Google Account -> Security).
2. Go to <https://myaccount.google.com/apppasswords>.
3. Create a new App Password (name it something like "Moodle notifier").
4. Copy the 16-character code — you won't see it again.

### 4. Add repository secrets

In your fork: **Settings -> Secrets and variables -> Actions -> New repository
secret**. Add these four:

| Secret name           | Value                                                |
|------------------------|-------------------------------------------------------|
| `ICAL_URL`             | The calendar URL from step 1                          |
| `GMAIL_ADDRESS`        | The Gmail account sending the notifications           |
| `GMAIL_APP_PASSWORD`   | The App Password from step 3                          |
| `RECIPIENT_EMAIL`      | Where notifications should be sent (can be the same address) |

### 5. First run (bootstrap)

Go to the **Actions** tab -> "Moodle assignment check" -> **Run workflow**.

The very first run is a silent bootstrap: it records every assignment
currently in your feed *without* emailing you (otherwise you'd get flooded
with everything that already exists). Check the run's logs — it should say
something like "Bootstrap complete: recorded N existing event(s)."

From the next run onward, only genuinely new events trigger an email.

### 6. Set up reliable 30-minute triggering with cron-job.org

1. Sign up free at <https://cron-job.org>.
2. Create a new cron job:
   - **URL:** `https://api.github.com/repos/<your-username>/<your-repo>/dispatches`
   - **Schedule:** every 30 minutes
   - **Request method:** `POST`
   - **Headers:**
     - `Authorization: Bearer <a GitHub Personal Access Token>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
   - **Body:** `{"event_type": "moodle-check"}`

   For the token: GitHub -> Settings -> Developer settings -> **Personal
   access tokens (classic)** -> Generate new token -> check the `repo` scope
   -> Generate. Copy it into the header above.

3. Save, then use cron-job.org's "Test run" button. Check your repo's
   Actions tab — a new run should appear with event type
   **"repository_dispatch"**, confirming the whole chain works.

### 7. Verify it's actually running

Let it sit for a couple of hours, then check the Actions tab — you should see
a steady stream of runs roughly 30 minutes apart. That's the real proof it's
alive and unattended.

## Useful flags

`moodle_notifier.py` is configured entirely through the four environment
variables from step 4 (no config file) -- the workflow sets these
automatically from your repo secrets. If you ever want to run it by hand
(e.g. to debug locally), export those same four variables in your shell
first, then:

- `python moodle_notifier.py --test-email` — sends a test email, ignoring
  Moodle entirely. Good for isolating Gmail/SMTP problems.
- `python moodle_notifier.py --dump-events` — prints the raw fields of your
  first few calendar events. Handy if course names aren't showing up in your
  emails — Moodle versions vary in whether they use `CATEGORIES`,
  `DESCRIPTION`, or something else to carry the course name, and this shows
  you exactly what your feed provides.
- `python moodle_notifier.py --rebootstrap` — wipes `seen_events.json` and
  silently re-syncs on the next run.

## Troubleshooting

**No email arriving at all:**
- Check spam/junk.
- Run `--test-email` locally, or check the workflow's run logs on GitHub —
  a wrong Gmail App Password is the most common cause.
- Confirm the secret names match exactly (`ICAL_URL`, `GMAIL_ADDRESS`,
  `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`) — GitHub Actions secrets are
  case-sensitive.

**cron-job.org test run returns a non-204 status:**
- `401` — the PAT is missing or malformed (check for "Bearer " with a space
  before the token).
- `403` — the PAT doesn't have the `repo` scope.
- `404` — typo in the URL (wrong username or repo name).
- `422` — the JSON body is malformed, or `event_type` doesn't match what's
  in `.github/workflows/moodle-check.yml`.

**Course name missing from emails:**
Run `python moodle_notifier.py --dump-events` (with your real env vars
exported) and check which field actually holds the course name in your
Moodle's feed — it's usually `CATEGORIES`, but not guaranteed across every
Moodle version/theme. Open an issue or adjust `build_email_body()` in
`moodle_notifier.py` if yours differs.

**Getting flooded with "new" notifications for things that already existed:**
This means `seen_events.json` didn't exist (or was reset) before a run with
events already in the feed. It's harmless after the fact, but if it happens
repeatedly, make sure your workflow's "Commit updated state" step is actually
allowed to push (check the `permissions: contents: write` block in the
workflow file is intact, and that nothing else is fighting over pushes to
`main`).

## Security notes

- Your Moodle calendar URL contains an access token. It's stored only as a
  GitHub Actions secret (encrypted, never shown in logs) — never commit it
  to the repo.
- The Gmail App Password only grants SMTP send access, not full account
  access, and can be revoked any time at
  <https://myaccount.google.com/apppasswords> without changing your main
  password.
- This repo's code is public; your secrets are not. The workflow only runs
  from scheduled/dispatch/manual triggers on this repo directly — it does
  not run automatically on pull requests from forks, so outside
  contributors can't use a PR to exfiltrate your secrets.

## Limitations

- Only catches assignments/events Moodle actually attaches to the calendar
  with a due date — something posted with no due date won't show up.
- Worst-case delay is whatever your polling interval is (30 minutes here) —
  this is polling, not a real-time push from Moodle.
- Depends on cron-job.org's free tier remaining available. If it ever
  disappears, swap in any other free "HTTP request on a schedule" service —
  the GitHub side (the `repository_dispatch` trigger) doesn't care who calls
  it.

## License

MIT — see [LICENSE](LICENSE).
