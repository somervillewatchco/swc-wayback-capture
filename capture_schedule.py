"""Gate the existing noon EST workflow: capture only after today's primary fails."""
import datetime as dt
import json
import os
from pathlib import Path
import urllib.request


PRIMARY_TITLE = 'Cart and policies - primary'
PRIMARY_CRON = '0 9 * * *'
RETRY_CRON = '0 17 * * *'


def retry_decision(runs, today):
    candidates = []
    for run in runs:
        if (run.get('event') != 'schedule' or run.get('head_branch') != 'main'
                or run.get('display_title') != PRIMARY_TITLE):
            continue
        created = dt.datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
        if created.date() == today:
            candidates.append(run)
    if not candidates:
        return False, 'No primary run recorded for this UTC day; retry skipped.'
    primary = max(candidates, key=lambda row: (row['created_at'], row['id']))
    if primary.get('status') != 'completed':
        return False, 'Primary is not completed; retry skipped.'
    if primary.get('conclusion') in ('failure', 'timed_out', 'startup_failure'):
        return True, 'Primary run %s failed; one scheduled retry is eligible.' % primary['id']
    return False, 'Primary conclusion is %s; retry skipped.' % primary.get('conclusion')


def main():
    if os.environ.get('GITHUB_EVENT_NAME') != 'schedule' or os.environ.get('SCHEDULE') == PRIMARY_CRON:
        allowed, reason = True, 'Primary or explicitly requested manual cart/policy run.'
    elif os.environ.get('SCHEDULE') == RETRY_CRON:
        repo = os.environ['GITHUB_REPOSITORY']
        request = urllib.request.Request(
            'https://api.github.com/repos/' + repo
            + '/actions/workflows/wayback-snapshot.yml/runs?event=schedule&branch=main&per_page=100',
            headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                     'Accept': 'application/vnd.github+json', 'User-Agent': 'swc-policy-schedule'})
        # An unavailable/invalid API response fails the gate, never authorizes capture.
        with urllib.request.urlopen(request, timeout=30) as response:
            runs = json.load(response)['workflow_runs']
        allowed, reason = retry_decision(runs, dt.datetime.now(dt.timezone.utc).date())
    else:
        allowed, reason = False, 'Superseded schedule; capture skipped.'
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write('run_capture=%s\n' % str(allowed).lower())
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
            summary.write('Schedule decision: ' + reason + '\n')
    print(reason)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
