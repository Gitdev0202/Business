# Automated Monitor

Runs a set of scheduled background checks against an external listings
source and forwards new, relevant results to a private notification
channel. Fully automated via cloud-based scheduled jobs — no local machine
needs to stay on.

## How it works

Each monitor runs on its own fixed interval, checks for new items matching
its own configured criteria, and keeps track of what has already been
reported so nothing gets sent twice. New matches are relayed to a private
webhook.

The schedules live in `.github/workflows/`, and the filter logic for each
monitor lives in its own script under `scripts/` — every script documents
its own specific criteria in the comment block at the top of the file.

## Setup

1. Create a webhook endpoint on the notification platform of your choice.
2. Add it as a repository secret (see the workflow files under
   `.github/workflows/` for the expected secret name(s)).
3. Enable the workflows under the repository's **Actions** tab.
4. Trigger one manually first (**Actions** → pick a workflow → **Run
   workflow**) to confirm it's wired up correctly before waiting for the
   schedule.

## Notes

- **Interval**: bound by the platform's minimum scheduling granularity;
  expect occasional delays during high load.
- **Repository visibility**: keep this repository **Public**. Several jobs
  run on short, frequent schedules, and a private repository has a monthly
  cap on automation minutes that a setup like this would exceed — jobs
  would then silently stop running. Public repositories aren't subject to
  that cap. No credentials live in the code itself, only as encrypted
  secrets, so public visibility doesn't expose anything sensitive.
- **Adjusting behavior**: thresholds and filters live at the top of each
  script in `scripts/`; change a value and commit, the next scheduled run
  picks it up automatically.
- If a scheduled job starts failing repeatedly, check the **Actions** tab
  for details.
