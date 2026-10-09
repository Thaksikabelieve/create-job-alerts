# Fresh career alerts repository — setup for Thaksika

This project scans the configured 30 company portals and emails one report after every completed check, including when no new jobs match. It requests scheduled runs every ten minutes. GitHub can delay or drop scheduled runs, so this is not a guaranteed ten-minute service.

## Recipient

Use this latest address for ALERT_EMAIL: thaksikabelive@gmail.com

You previously used thaksikabelieve@gmail.com (with an extra e in believe). This package uses your latest instruction. Check the spelling before saving the secret. The SMTP sender may be a different Gmail account; the app password must belong to that sender.

## Email criteria

A job must explicitly allow India or worldwide work, and its DESCRIPTION must contain at least ONE keyword. Matching ignores capitalization:

linux, github, gitlab, aws, devops, cloud, Oracle Linux Virtualization Manager, OLVM, Oracle Linux Automation Manager, OLAM, playbook, citrix, azure, vmware, Active Directory, windows, ansible.

Remote India qualifies. Generic Remote, Remote US and Remote Europe do not establish worldwide eligibility. India country-code fields such as Bengaluru, KA,IN, IN are supported. Edit filters.json to change keywords.

## 1. Create a new GitHub repository

1. Sign in at https://github.com using your Thaksikabelieve account.
2. Click the + menu near the top right, then New repository.
3. Repository name: career-alerts-10min
4. Choose Public if you want to use free standard GitHub-hosted runners. Gmail passwords belong only in Secrets, never in repository files.
5. Tick Add a README file.
6. Click Create repository.

This ZIP is ready to upload; it does not create the repository in your account automatically.

## 2. Upload the project files

1. Download career-alerts-fresh.zip, right-click it, and choose Extract All.
2. Open the extracted folder. The actual project files are at its root.
3. Open your new repository's main file list in your browser.
4. Click Add file > Upload files.
5. Drag these SIX files into the upload area:
   check_jobs.py
   job_filters.py
   filters.json
   portals.json
   requirements.txt
   README.md
6. Commit changes to main. Replace the starter README with the provided README.

Do not upload the ZIP itself, browser files, or any state/jobs.json from the old repository. The new project creates its own job history on its first successful scan. The tests directory is optional for running the workflow.

## 3. Create the workflow file

The workflow must be inside .github/workflows; uploading it to the main folder will not work.

1. In the repository main file list, click Add file > Create new file.
2. In the filename box, type exactly:
   .github/workflows/check-jobs.yml
3. Paste the workflow shown at the end of this README into the large editor, without the Markdown backticks.
4. Click Commit changes and commit to main.

Alternatively, open the included .github/workflows/check-jobs.yml in Notepad and copy its contents into that editor.

## 4. Add email secrets to the NEW repository

Secrets from your old repository do not automatically carry over.

1. Open Settings > Secrets and variables > Actions.
2. Click New repository secret for EACH of these:

| Name | Value |
|---|---|
| SMTP_USER | Full Gmail address of the account sending emails. Example: your existing thaksikabelieve@gmail.com sender, if that is the account your app password belongs to. |
| SMTP_PASSWORD | That sender account's Google app password, not the normal account password. Remove spaces when pasting. |
| ALERT_EMAIL | thaksikabelive@gmail.com |

Keep the secret names exact. Do not paste passwords into Python files, issues, or chat. SMTP_HOST and SMTP_PORT are optional; the script defaults to smtp.gmail.com and SSL port 465.

If you do not have an app password, use the same Gmail app-password setup used for your previous repository. An app password from a different account will not authenticate SMTP_USER.

## 5. Run and inspect the first check

1. Click Actions.
2. If GitHub asks whether to enable workflows, enable them.
3. Select Career checks and email reports in the left list.
4. Click Run workflow, select main, and keep send_test_email OFF.
5. Click the green Run workflow button.
6. Open the new run, select check, and expand Check career portals and send email.
7. Wait until the run finishes. Check the recipient inbox and Spam folder.

You should receive a check report even if no new jobs match. The first successful scan of each company saves a BASELINE: existing jobs are recorded without sending them as new-job alerts. The report says this explicitly. Later detected matching links appear in subsequent report emails.

After successful setup, disable your OLD workflow if you want to stop old emails: open the OLD repository > Actions > its workflow > the three-dot menu > Disable workflow. Keep the new workflow enabled.

## 6. Automatic checking

The cron expression is 3/10 * * * *: checks are requested at minutes 03, 13, 23, 33, 43 and 53 of each hour. Scheduled runs use the repository default branch, so keep this workflow on main and make main the default branch.

Manual Run workflow starts only one check. You do not need to keep your computer on for GitHub-hosted runs. Check Actions for entries labelled Scheduled. GitHub scheduling is best effort; a fresh repository does not guarantee a fix for platform scheduling delays.

## What every report contains

- Date and time in India.
- Total newly detected matching jobs.
- A result and portal link for EACH of the 30 companies.
- Matching job title, job URL, location and matched keywords.
- No new job matching your required criteria detected for a successful, established scan with no match.
- BASELINE SAVED on initial successful scans.
- CHECK FAILED / CHECK INCOMPLETE when a portal could not be read or no links could be extracted: the absence of new jobs is unknown.
- Pending-description counts when a newly found job's details could not be read; those links are retried.

The report is mailed after scanning and filtering, before the workflow saves history. Its wording confirms that these script steps finished, not that every company was accessible or that the following history commit succeeded. If SMTP authentication fails, or installation/script execution fails before report generation, no normal report can be sent. Inspect Actions when a report is missing. Manual setup-test mode sends an extra setup email before the regular report.

## Limits

- Detected links are not proof of complete coverage. Some portals restrict access, paginate, or use unsupported layouts. This project uses bounded crawling and public Greenhouse feeds where available.
- New means newly detected after baseline. Original publication dates are not verified, and changes at an already recorded URL are not new alerts.
- Jobs whose location does not clearly permit India/worldwide work are excluded. Unknown description reads remain pending.
- Nonmatching jobs are stored in history to avoid checking them repeatedly. Changing your keyword list does not replay previously seen jobs.
- SMTP errors retain the old history so alerts can retry. If an email succeeds but history saving subsequently fails, later runs can repeat that job alert.
- All 18 local regression tests passed. Full live coverage of all 30 portals, inbox delivery and this new repository's scheduling have not been verified.

## Optional tests

Upload the tests folder as well if you want it in your repository. With Python installed, run from the project folder:

    python -m unittest discover -s tests -v

## Workflow to paste

```yaml
name: Career checks and email reports

on:
  schedule:
    - cron: '3/10 * * * *'
  workflow_dispatch:
    inputs:
      send_test_email:
        description: 'Send a setup test email before checking jobs'
        type: boolean
        default: false

permissions:
  contents: write

concurrency:
  group: career-alerts
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: true
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
          cache-dependency-path: requirements.txt
      - run: pip install -r requirements.txt
      - run: python -m playwright install --with-deps chromium
      - name: Check career portals and send email
        env:
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
          ALERT_EMAIL: ${{ secrets.ALERT_EMAIL }}
          SMTP_HOST: ${{ vars.SMTP_HOST }}
          SMTP_PORT: ${{ vars.SMTP_PORT }}
          SEND_TEST_EMAIL: ${{ inputs.send_test_email }}
        run: python check_jobs.py
      - name: Save job history
        run: |
          if [ ! -f state/jobs.json ]; then
            echo 'No portal baseline was created. Review the portal results and scan logs.'
            exit 1
          fi
          git add state/jobs.json
          if ! git diff --cached --quiet; then
            git -c user.name='github-actions[bot]' -c user.email='41898282+github-actions[bot]@users.noreply.github.com' commit -m 'Update career job baseline'
            git push
          fi
```
