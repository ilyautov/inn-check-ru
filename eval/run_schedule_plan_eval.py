#!/usr/bin/env python3
"""Scheduler templates are config only; generated argv is host executable."""
import json
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import monitor_events
import schedule_templates
import workflow_cli


class ScheduleTests(unittest.TestCase):
    def test_templates_and_pure_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / 'spaces & more'
            for platform in ('linux', 'macos', 'windows'):
                with patch('subprocess.Popen', side_effect=AssertionError('scheduler must not run')):
                    plan = monitor_events.host_plan('нейтрально', now='2026-10-16T09:00:00Z', platform=platform, root=root)
                self.assertFalse(root.exists())
                self.assertFalse(plan['installed'])
                self.assertEqual(plan['limits']['fetch_timeout_seconds'], 120)
                self.assertLessEqual(plan['max_subjects'], 50)
                self.assertEqual(plistlib.loads(plan['plist'].encode())['ProgramArguments'], plan['argv'])
                self.assertEqual(shlex.split(plan['cron'].split(' ', 5)[5]), plan['argv'])
                tree = ET.fromstring(plan['task_xml']); ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
                self.assertEqual(tree.find('.//t:Command', ns).text, plan['argv'][0])
                self.assertEqual(tree.find('.//t:Arguments', ns).text, subprocess.list2cmdline(plan['argv'][1:]))
                self.assertEqual(tree.find('.//t:StartWhenAvailable', ns).text, 'true')
                self.assertEqual(tree.find('.//t:MultipleInstancesPolicy', ns).text, 'IgnoreNew')
                self.assertEqual(tree.find('.//t:ExecutionTimeLimit', ns).text, 'PT6060S')
                self.assertEqual(tree.find('.//t:RestartOnFailure/t:Count', ns).text, '2')
                args = workflow_cli.parser().parse_args(plan['argv'][2:])
                self.assertEqual(args.watch_command, 'monitor')
    def test_default_queue_matches_mcp(self):
        with patch.dict('os.environ', {}, clear=True), patch('cache_policy.plan', return_value={'datasets': [], 'minimal_actions': []}):
            plan = monitor_events.host_plan('нейтрально', now='2026-10-16T09:00:00Z', platform='linux')
            self.assertEqual(plan['event_output'], str(monitor_events._base(None) / 'events.json'))

    def test_n8n_sample_is_config_only(self):
        sample = json.loads((Path(__file__).resolve().parents[1] / 'examples/n8n-monitoring.json').read_text())
        self.assertFalse(sample['active'])
        nodes = sample['nodes']
        self.assertEqual(nodes[0]['type'], 'n8n-nodes-base.manualTrigger')
        self.assertFalse(any('credentials' in node for node in nodes))
        self.assertFalse(any(node['type'] in {'n8n-nodes-base.httpRequest', 'n8n-nodes-base.executeCommand'} for node in nodes))
        action = next(x for x in nodes if x['name'] == 'Configure host action adapter')
        self.assertIn('throw new Error', action['parameters']['jsCode'])
        ack = next(x for x in nodes if x['name'] == 'Explicit ack request after success')
        self.assertIn('host_action_succeeded !== true', ack['parameters']['jsCode'])
        self.assertIn("'events', 'ack'", ack['parameters']['jsCode'])

    def test_actual_n8n_lease_validator(self):
        sample = json.loads((Path(__file__).resolve().parents[1] / 'examples/n8n-monitoring.json').read_text())
        code = next(n['parameters']['jsCode'] for n in sample['nodes'] if n['name'] == 'Validate leases and event identities')
        harness = r"""
const fs = require('fs');
const code = JSON.parse(fs.readFileSync(0, 'utf8'));
const run = new (Object.getPrototypeOf(async function(){}).constructor)('$', code);
const config = {consumer_id: 'local-n8n'};
(async () => {
 const results = [];
 for (const expiry of ['not-a-date', null, undefined, 42, '2026-10-16T08:59:59Z', '2026-10-16T09:00:00Z', '2026-10-16T09:00:01Z']) {
  Date.now = () => Date.parse('2026-10-16T09:00:00Z');
  const event = {event_id: 'a'.repeat(64), delivery: {state: 'delivered', consumer_id: config.consumer_id, lease_expires_at: expiry}};
  const context = {helpers: {getBinaryDataBuffer: async () => Buffer.from(JSON.stringify({status:'ok', data:[event]}))}};
  try { const items = await run.call(context, () => ({first: () => ({json: config})})); results.push(items.length); }
  catch (_) { results.push('rejected'); }
 }
 process.stdout.write(JSON.stringify(results));
})();
"""
        result = subprocess.run(['node', '-e', harness], input=json.dumps(code), capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['rejected'] * 6 + [1])

    def test_timezone_and_budget(self):
        template = schedule_templates.render(['/usr/bin/python3', '/path with &/script.py'], interval_days=1,
            start_at='2026-10-16T09:20:00Z', timeout_seconds=120)
        root = ET.fromstring(template['windows_xml'])
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(root.find('.//t:StartBoundary', ns).text, '2026-10-16T09:20:00Z')
        for argv in (['x\ny'], ['']):
            with self.assertRaises(ValueError): schedule_templates.render(argv, interval_days=1)


if __name__ == '__main__': unittest.main(verbosity=2)
