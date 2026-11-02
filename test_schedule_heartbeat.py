"""Offline workflow constraints and shell date guards; no GitHub dispatch."""
import os
from pathlib import Path
import re
import subprocess
import unittest

WORKFLOW=Path(__file__).resolve().parent/'.github/workflows/wayback-schedule-heartbeat.yml'

class HeartbeatTests(unittest.TestCase):
 def shell(self,now,event='schedule'):
  text=WORKFLOW.read_text();script=text.split('        run: |\n',1)[1]
  script='\n'.join(line[10:] for line in script.splitlines())
  # Shadow only the local date command, never invoke networking/Actions.
  prefix='date() { printf "%s\\n" "$MOCK_NOW"; };\n'
  return subprocess.run(['bash','-c',prefix+script],env={'PATH':os.environ['PATH'],'MOCK_NOW':now,'EVENT_NAME':event},capture_output=True,text=True,check=True).stdout
 def test_fixed_cron_and_no_production_or_network_surface(self):
  text=WORKFLOW.read_text()
  self.assertIn("cron: '7 17 2 11 *'",text)
  self.assertEqual(text.count('cron:'),1)
  self.assertIn('permissions: {}',text);self.assertIn('timeout-minutes: 2',text)
  for forbidden in ('uses:','secrets.','actions/cache','upload-artifact','checkout','curl','wget','https://','capture.py','concurrency:','GITHUB_TOKEN'):
   self.assertNotIn(forbidden,text)
  self.assertIn('workflow_dispatch: {}',text)
 def test_scheduled_and_delayed_opportunity_within_window(self):
  for now in ('2026-11-02T17:07:00Z','2026-11-02T23:59:59Z','2026-11-03T01:30:00Z'):
   output=self.shell(now);self.assertIn('Scheduler heartbeat',output)
   self.assertIn('event=schedule',output);self.assertIn(now,output)
 def test_future_year_and_past_window_are_noop(self):
  for now in ('2025-11-02T17:07:00Z','2026-11-03T01:30:01Z','2027-11-02T17:07:00Z'):
   output=self.shell(now);self.assertIn('Heartbeat expired; no diagnostic work.',output)
   self.assertNotIn('Scheduler heartbeat',output)
 def test_manual_event_is_distinct_not_claimed_scheduled(self):
  output=self.shell('2026-11-02T16:40:00Z','workflow_dispatch')
  self.assertIn('event=workflow_dispatch',output);self.assertNotIn('event=schedule',output)

if __name__=='__main__':unittest.main()
