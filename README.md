# Career portal email alerts

Checks the 30 company career portals in `portals.json` for newly discovered job links. On its first successful scan of each company, the script saves a baseline without emailing existing jobs. Later scans send one email listing newly discovered jobs and update `state/jobs.json` after the email succeeds.

## Set up in your GitHub repository

1. Create a **public** GitHub repository to use free standard GitHub-hosted runners, and upload the **contents** of this folder at the repository root, including `.github/workflows/check-jobs.yml`. The source code and saved public job links will be public. Keep email addresses and passwords in GitHub Secrets as described below.
2. In **Settings → Secrets and variables → Actions → Repository secrets**, add:

   | Secret | Value |
   | --- | --- |
   | `SMTP_USER` | Sender's email address (for example, your Gmail address). |
   | `SMTP_PASSWORD` | SMTP password. For Gmail, use a Google app password on an account with two-step verification. |
   | `ALERT_EMAIL` | The address that should receive alerts. It may equal `SMTP_USER`. |

3. The default SMTP server is Gmail (`smtp.gmail.com`, port `465`). For another provider, set repository **variables** `SMTP_HOST` and `SMTP_PORT` for its SSL SMTP endpoint.
4. In **Settings → Actions → General → Workflow permissions**, allow **Read and write permissions**, so the workflow can commit `state/jobs.json`. Ensure Actions are enabled for the repository.
5. Open **Actions → Career job email alerts → Run workflow**, select **Send a setup test email before checking jobs**, and run it. You should receive an email with subject **Career alerts: test email**. Review the run's **Summary** for a results table covering all 30 portals, and its logs for failed or empty company scans. It creates the baseline on the first successful scan of each portal. Subsequent runs email any newly seen links. Scheduled runs do not send the setup test email.

The workflow requests runs every five minutes (`2/5 * * * *`, UTC). GitHub may delay scheduled runs, and scans can take several minutes. There is no guarantee of an alert within five minutes of a posting. Keeping the job state in the repository allows successive runners to compare results; do not delete `state/jobs.json` unless you intend to reset the baseline.

## Keep GitHub hosting free

The supplied workflow uses the standard `ubuntu-latest` runner, which is free for public repositories. No paid monitoring service is required. Private repositories have a monthly runner allowance; GitHub Free includes 2,000 minutes. A five-minute schedule requests about 8,640 runs in a 30-day month, so this workload can exceed that private-repository allowance even if each run only uses one minute. Use a public repository for this setup, or run it on a machine you already have. Your email provider's own SMTP limits still apply.

Official references: [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [runner limits](https://docs.github.com/en/actions/reference/limits), and [scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
export SMTP_USER='sender@example.com'
export SMTP_PASSWORD='your-app-password'
export ALERT_EMAIL='recipient@example.com'
python check_jobs.py
python -m unittest discover -s tests -v
```

For a local timer, keep `state/jobs.json` on persistent storage and ensure only one invocation runs at a time. The GitHub workflow already serializes its runs.

## Coverage and troubleshooting

This is a browser-based best-effort monitor of the official listing pages and their linked job boards. It scans at most four pages per company on each run, then recognizes direct job URLs by common ATS and career-site patterns. It can miss jobs behind pagination, search-only interfaces, bot protection, or layouts with changed links. It cannot certify that it catches **every** posting on all 30 portals. A site with zero recognized job links keeps its old baseline and is reported in the workflow logs; check those logs after the first run and periodically thereafter. A link newly discovered after a site redesign may appear as a new job even if the posting is old.

To add or remove a company, edit `portals.json`. Job IDs are tracked per company. To change recognition rules, edit `JOB_PATHS` and `is_job` in `check_jobs.py` and run the tests. SMTP errors cause the run to fail before state is saved so it can retry on the next run; mail delivery may still be duplicated if a provider accepted a message but its confirmation was lost.
