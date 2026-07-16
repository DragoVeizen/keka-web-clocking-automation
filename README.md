# Keka Web Clocking Automation

Automatically clock in and out of the [Keka](https://www.keka.com/) web app on a
schedule. A single Python script (`keka_script.py`) drives a headless Chrome
session: it logs in (solving the login captcha and Keka's two‑step email OTP
automatically), then clicks **Web Clock‑In** / **Web Clock‑out**. It can run in
the cloud on **GitHub Actions** (free, always‑on) or locally via **macOS
launchd** / **Linux systemd**.

---

## Contents

- [How it works](#how-it-works)
- [Configuration (environment variables)](#configuration-environment-variables)
- [Run it on GitHub Actions (recommended)](#run-it-on-github-actions-recommended)
- [Run it locally (macOS launchd)](#run-it-locally-macos-launchd)
- [Scheduling & GitHub Actions delays](#scheduling--github-actions-delays)
- [Manual / one‑off runs](#manual--one-off-runs)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Repository layout](#repository-layout)

---

## How it works

Each run performs, in order:

1. **Open the dashboard.** Loads a persistent Chrome profile
   (`~/.keka-chrome-profile`). If a saved session is still valid, Keka goes
   straight to the dashboard and **steps 2–4 are skipped entirely**.
2. **Log in** (only when the session has expired):
   - **Captcha.** The login captcha image is read by a vision model. Solvers are
     tried best‑first: an **OpenAI‑compatible vision API** (GitHub Models
     `gpt-4o-mini` by default) → **Gemini** → **Tesseract** OCR. The login loop
     retries up to 5× with fresh captchas, so an occasional misread is fine.
   - **Two‑step 2FA.** Keka shows a *"Verify your identity"* page; the script
     clicks **Send code to email**, which triggers a One‑Time Password email.
   - **OTP.** The 6‑digit code is read over **IMAP** from a mailbox you control
     (see [OTP by email](#otp-by-email)), then entered and submitted.
3. **Clock.** Navigates to the attendance page, dismisses any onboarding popup,
   and clicks **Web Clock‑In** or **Web Clock‑out** (with a JavaScript‑click
   fallback so overlays can't block it). The action is idempotent — if you're
   already clocked in/out it does nothing.
4. **Notify.** Optionally emails a success/failure summary via SMTP.

### OTP by email

Keka has 2FA enabled, so a fresh login always requires an emailed OTP (from
`no-reply@kekamail.com`, subject *"One-Time Password (OTP) for Secure Login"*,
body `OTP: NNNNNN`, valid 10 min). The script reads that email over IMAP.

Because work Google Workspace accounts often block IMAP/app‑passwords, the
recommended setup is to **forward the OTP emails to a personal Gmail** and read
that instead:

1. In the Keka account's mailbox, add your personal Gmail as a verified
   forwarding address, then create a filter:
   `From: no-reply@kekamail.com`, `Subject: One-Time Password (OTP) for Secure Login`
   → **Forward to** your personal Gmail (also tick *Never send to Spam*).
2. On the personal Gmail, create an [app password](https://myaccount.google.com/apppasswords)
   (needs 2‑step verification). The script uses it for IMAP — the same app
   password also works for SMTP status emails.

### Captcha solving on CI

On GitHub Actions the captcha solver defaults to **GitHub Models**
(`openai/gpt-4o-mini`) authenticated with the workflow's built‑in `GITHUB_TOKEN`
(no extra API key or secret required — just `permissions: models: read`). The
free Gemini tier is unreliable for this (frequent `429`/`503`), so it's only a
fallback. The solver is a generic OpenAI‑compatible client, so you can point it
at Groq, OpenRouter, etc. by changing `VISION_BASE_URL` / `VISION_MODEL` /
`VISION_API_KEY`.

---

## Configuration (environment variables)

Copy `.env.sample` to `.env` for local runs, or set these as **repository
secrets** for GitHub Actions. `.env` is git‑ignored and must never be committed.

| Variable | Required | Description |
|---|---|---|
| `KEKA_EMAIL` | ✅ | Keka login email |
| `KEKA_PASSWORD` | ✅ | Keka password |
| `KEKA_URL` | ✅ | Your Keka tenant URL, e.g. `https://yourco.keka.com` |
| `KEKA_CHECK` | ✅ | `in` or `out` (set per run / per schedule) |
| `KEKA_HEADLESS` | – | `1` to run headless (required on servers/CI) |
| `VISION_BASE_URL` | – | OpenAI‑compatible endpoint, e.g. `https://models.github.ai/inference` |
| `VISION_API_KEY` | – | Token for the vision endpoint (on CI: `GITHUB_TOKEN`) |
| `VISION_MODEL` | – | Vision model id (default `openai/gpt-4o-mini`) |
| `GEMINI_API_KEY` | – | Fallback captcha solver (AI Studio key, `AIza…`) |
| `GEMINI_MODELS` | – | Comma‑separated fallback list (default `gemini-2.0-flash,gemini-3.5-flash,gemini-flash-latest`) |
| `SMTP_USER` | – | Gmail for status emails **and** default IMAP OTP reader |
| `SMTP_PASSWORD` | – | Gmail app password (used for SMTP and IMAP) |
| `NOTIFY_EMAIL` | – | Where to send run status emails |
| `IMAP_HOST` | – | Default `imap.gmail.com` |
| `IMAP_USER` / `IMAP_PASSWORD` | – | Override the OTP mailbox (defaults to `SMTP_USER`/`SMTP_PASSWORD`) |
| `OTP_SUBJECT` | – | Subject substring to match (default `One-Time Password`) |
| `OTP_TIMEOUT` | – | Seconds to wait for the OTP email (default `150`) |

---

## Run it on GitHub Actions (recommended)

Free, always‑on, no machine to keep awake. The workflow lives in
`.github/workflows/clock.yml`.

1. **Push to your own repo.** Use a **private** repo (secrets + attendance
   automation shouldn't be public).
2. **Set up OTP forwarding** as described in [OTP by email](#otp-by-email).
3. **Add repository secrets** (Settings → Secrets and variables → Actions):
   `KEKA_EMAIL`, `KEKA_PASSWORD`, `KEKA_URL`, `SMTP_USER`, `SMTP_PASSWORD`,
   `NOTIFY_EMAIL` (and optionally `GEMINI_API_KEY`).
   The captcha solver uses GitHub Models via the built‑in `GITHUB_TOKEN`, so no
   vision key is needed.
4. **Done.** The schedule runs Mon–Fri automatically. The first run solves the
   captcha + OTP; its session is cached (`actions/cache`) so later runs skip
   both.

> **Note:** GitHub disables scheduled workflows after **60 days of no repo
> commits**. Push a trivial commit occasionally (or add a keepalive workflow) to
> keep it active.

---

## Run it locally (macOS launchd)

For a local, residential‑IP setup (which rarely triggers captcha/OTP thanks to
the persistent profile):

1. Install dependencies and a browser:
   ```
   pip install -r requirements.txt          # selenium, pillow, pytesseract, python-dotenv
   brew install tesseract                    # optional OCR fallback
   ```
   Google Chrome must be installed; Selenium Manager fetches the matching driver.
2. Create `.env` (see [Configuration](#configuration-environment-variables)).
3. Install the launch agents at `~/Library/LaunchAgents/`
   (`com.hudle.keka.clockin.plist`, `com.hudle.keka.clockout.plist`) with a
   `StartCalendarInterval` for your times, then load them:
   ```
   launchctl load ~/Library/LaunchAgents/com.hudle.keka.clockin.plist
   launchctl load ~/Library/LaunchAgents/com.hudle.keka.clockout.plist
   ```
   To stop: `launchctl unload <plist>`.

The machine must be awake at the scheduled times. Linux users can adapt the
systemd unit files in `services/`.

---

## Scheduling & GitHub Actions delays

Default schedule (`.github/workflows/clock.yml`), Mon–Fri:

| Action | Cron (UTC) | Time (IST) |
|---|---|---|
| Clock in | `10 4 * * 1-5` | ~09:40 |
| Clock out | `30 12 * * 1-5` | ~18:00 |

GitHub runs scheduled jobs **on time or late — never early**, and can be delayed
several minutes (occasionally 15–30 under load). The times are chosen so delays
are harmless:

- **Clock‑in at 09:40** leaves a buffer before a 10:00 start; a delay just drifts
  it toward 10:00.
- **Clock‑out at 18:00** can only drift *later*, which merely logs a little more
  time.
- The workflow decides in‑vs‑out from the **UTC hour** (morning → in, afternoon →
  out), so a delayed run can never pick the wrong action.

IST has no daylight‑saving, so these UTC times are stable year‑round. To change
them, edit the two `cron:` lines (remember they're UTC = IST − 5:30).

---

## Manual / one‑off runs

Locally:
```
KEKA_CHECK=in  KEKA_HEADLESS=1 python keka_script.py   # clock in
KEKA_CHECK=out KEKA_HEADLESS=1 python keka_script.py   # clock out
```

On GitHub Actions: **Actions → Keka Clock → Run workflow**, pick `in` or `out`.

---

## Troubleshooting

- **`Invalid Captcha` / login fails every attempt** — the vision model is
  misreading. Try a stronger `VISION_MODEL` (e.g. `openai/gpt-4o`) or provide a
  valid `GEMINI_API_KEY`. Occasional misreads are normal; the loop retries.
- **`no OTP email arrived within Ns`** — OTP forwarding isn't reaching the IMAP
  mailbox. Verify the Gmail filter and that IMAP + the app password work.
- **`ElementClickInterceptedException`** — a popup is covering the button. The
  script already dismisses known popups and falls back to a JS click; if a new
  popup appears, check the `logs/*.html` dump.
- **Scheduled run didn't fire** — GitHub delayed or dropped it under load, or the
  workflow was auto‑disabled after 60 days of no commits. Re‑enable it in the
  Actions tab and/or push a commit.
- **Debugging CI** — failed runs upload the page HTML from `logs/` as an
  artifact; the run log also prints the captcha read and Keka's error text.

---

## Security notes

- `.env` is git‑ignored; keep real credentials out of the repo. Use a **private**
  repo and GitHub **secrets** for CI.
- Reading OTPs requires access to a mailbox — prefer forwarding to a dedicated /
  personal Gmail with an app password rather than exposing work‑account
  credentials.
- Logs and page dumps may contain session details; `logs/` is git‑ignored.

---

## Repository layout

```
keka_script.py              # the automation (login, captcha, 2FA/OTP, clock, notify)
requirements.txt            # Python dependencies
.env.sample                 # template for local configuration
.github/workflows/clock.yml # scheduled GitHub Actions workflow
services/                   # example Linux systemd unit files
logs/                       # run logs & debug dumps (git-ignored)
```

## License

[MIT License](LICENSE).
