# Turn on grant emails with Gmail

Use a dedicated Gmail account for the app. Enable Google 2-Step Verification,
then create an [app password](https://support.google.com/accounts/answer/185833).
Google may restrict app passwords on managed accounts; use an account that
offers this option. Keep the password out of source code, GitHub and chat.

In Azure Portal, open **App Services → ncgrantfinder → Settings → Environment
variables → App settings**. Add these values and apply the changes:

| Setting | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | The sender's full Gmail address |
| `SMTP_PASS` | The Gmail app password, not the account password |
| `SMTP_FROM` | The same Gmail address |
| `PUBLIC_BASE_URL` | `https://ncgrantfinder-bshjbjbgeahqhnck.westus3-01.azurewebsites.net` |
| `NOTIFICATIONS_ENABLED` | `1` (set `0` to pause automatic delivery) |

The app restarts when settings are applied. It uses encrypted SMTP with
STARTTLS. Gmail is suitable for a small pilot; check Google's
[sending limits](https://support.google.com/mail/answer/22839) before expanding.

## What staff receive

- **Pursued grants:** select Pursue, tick deadline reminders and enter an email.
  Reminders arrive once at each 30, 14, 7, 3 and 1 day stage. A late signup
  starts at the current stage, without sending earlier reminders together.
  Changing the shared decision to Watch or Pass stops that grant's reminders.
- **Weekly field updates:** select the organization or field, open Weekly
  briefing and choose Email me weekly updates. Emails arrive Monday mornings
  from 7 a.m. North Carolina time, starting the following Monday. They include
  grants added since signup or the previous update, plus matching deadlines
  within 30 days. A goal fit of at least 40 is required; explicitly ineligible
  and closed applications are excluded. Dated state and foundation applications
  are included when they fit the selection.
- **Confirmation:** click the email confirmation link to activate requests.
  The link lasts 48 hours from its delivery attempt. Previously saved addresses
  are not activated automatically. Each regular email has an unsubscribe link.

Without sender settings, requests stay queued and the app tells staff that
email setup is needed. `/api/notifications/status` reports readiness without
revealing credentials or subscriber addresses. Its `outbox` counts show how
many emails are pending, sent and failed: if `failed` keeps growing, the Gmail
app password or account is usually the problem.

## Scheduling and verification

The app checks its SQLite queue every minute while running. The **Scheduled
grant emails** GitHub Actions workflow also wakes the Azure app hourly in case
App Service has gone idle. It uses the existing Azure deployment identity;
mail credentials remain in Azure. The job reads or creates an `ADMIN_TOKEN`
app setting and calls the protected notification endpoint. Dispatch this
workflow manually after deployment to verify Azure access and readiness.

Subscriptions, delivery history and retries persist in `/home/data/matchmaker.db`.
Failed attempts back off from five minutes to six hours. A deadline change,
closed application or cancelled subscription is checked again before sending.
SQLite claims prevent two workers from processing the same queued email at
once. If a worker stops after SMTP accepts a message but before SQLite records
success, a retry can still duplicate that message.

For a delivery check, opt in using your own address, receive the confirmation,
and confirm it. Inspect the next workflow run and your inbox. Automated tests
use a fake sender and never send mail to real recipients.
