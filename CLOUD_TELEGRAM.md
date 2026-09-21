# QuantMix PC-independent Telegram notifications

The GitHub Actions workflow `jongsa-daily.yml` runs at 13:00 and 19:00 Korea
time on weekdays, once per slot. The old 21:10/22:10 schedule is removed.
It does not need the desktop launcher or Streamlit to stay awake.
GitHub may delay scheduled jobs; this is not an exact-time trading service.

## Account and security

- `QUANTMIX_PROFILE_JSON`: complete private settings, deposits/withdrawals,
  actual buy fills, frozen order quantities, and confirmed price overrides.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: existing owner bot/destination.
- `QUANTMIX_STATE_KEY`: Fernet authenticated-encryption key.
- All four are **Actions Secrets**, not public source files.
- `quantmix-notify-state/outbox.enc` contains only encrypted delivery history.
  The key is not stored on that branch. No private artifacts/caches are uploaded.
- Public logs contain success/failure codes, never order contents or balances.

The account is a reconstruction from the recorded inputs, NOT a brokerage API.
Actual sell discrepancies (including partial fills) must be reconciled before
placing an order. This deployment does not assume or invent missing fills.
An optional gitignored `jongsa_reconciliation.json` supplies a private
`notification_warning` shown above the orders until reconciliation is complete.

## Daily behavior

1. Target the US session with the Korean daytime calendar date, including at
   13:00 KST in winter when New York is still on the preceding date. Use the
   NYSE calendar for US holidays and shortened sessions. Skip closed days and
   expired slots/orders; the afternoon slot expires when the evening slot starts.
2. Require the preceding session's confirmed close. Missing data fails closed.
3. Calculate with the same engine and explicit account inputs as the local app.
4. Reserve the day/slot in the encrypted outbox, then send one copy-friendly message.
5. Record the Telegram message id and frozen quantity after successful delivery.

A rerun of an already delivered slot does not send it again. The second slot
reuses the exact order saved in the encrypted outbox, with an explicit reminder
not to place an additional order. Sent quantities remain one record per trading
day for local synchronization. Legacy days without a saved message cannot be
resent automatically. An ambiguous
delivery (e.g. timeout after Telegram accepted it) is **not blindly retried**:
the workflow fails and asks the owner to check. Never duplicate orders based on
a repeated notification. A failed run sends an error alert when Telegram is
reachable; infrastructure/Telegram outages can prevent even that alert.

## Local synchronization

Initial setup (owner machine, existing Git Credential Manager login):

```powershell
python -m pip install -r requirements-notify.txt PyNaCl==1.6.2
python scripts/sync_quantmix_cloud.py --initialize
```

`jongsa_cloud.json` is a **private gitignored file** holding the repository name
and the outbox key; preserve it. `jongsa_cloud_sync.json` records a local sync
digest. Neither stores a GitHub token. Do not share these files.

On a connected local app, cloud-sent quantities are loaded into the app and
changes to the saved account settings are automatically uploaded to the Secret.
The alert tab reports synchronization success/failure. On another PC, this is
available only after that PC's GitHub login and local connection are configured.
The public shared Streamlit app cannot alter the owner's private notification
profile; changes made only in that public browser session are **not synced**.

To explicitly upload settings without sending a notification:

```powershell
python scripts/sync_quantmix_cloud.py --force
```

## Verification / operation

- Run tests: `python -m unittest discover -s tests -p test_quantmix_cloud.py -v`.
- Actions → Run workflow → `dry_run=true`: validate without sending.
- `dry_run=false`: deliver for today's Korean-dated US session and the selected
  time slot (13:00 before 19:00 KST; 19:00 afterwards), if still before cutoff.
- Disabling the workflow stops scheduled delivery. It does not stop the local
  on-demand bot; the two are independent.
- A successful send proves API acceptance, not that a phone displayed/read it.
- This workflow places **no brokerage orders** and performs no strategy research.

References: [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[repository Secrets API](https://docs.github.com/en/rest/actions/secrets#create-or-update-a-repository-secret).
