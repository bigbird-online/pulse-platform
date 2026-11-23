from dataclasses import replace
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).parents[1]))
from pulse_stats import legacy_retirement as r
from pulse_stats.live_contract import _short_hash
from Stats.exporter.tests.test_live import LiveFixture,FakeRunner,ack_result,systemd_result,NOW

class RetirementTests(unittest.TestCase):
    def test_retired_history_is_preserved_not_recovered_and_not_current(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=LiveFixture(Path(directory))
            fixture.write_heartbeats(NOW-timedelta(days=10),NOW-timedelta(days=10))
            before=json.loads(fixture.service(FakeRunner(ack=ack_result(),systemd=systemd_result())).render_json())
            old=[i for i in before['incidents'] if i['state']=='open' and i['capability_id']=='meta.probe_heartbeat']
            self.assertGreater(len(old),0)
            registry=json.loads(fixture.registry.read_text())
            for source in registry['sources']:source['enabled']=False
            registry['sources'].append(dict(source='hestia-p0-probe',enabled=True,max_age_seconds=1500))
            fixture.registry.write_text(json.dumps(registry))
            fixture.heartbeats.write_text(json.dumps({'hestia-p0-probe':{'receivedAt':NOW.isoformat()}}))
            fixture.paths=replace(fixture.paths,retire_legacy_probes=True)
            service=fixture.service(FakeRunner(ack=ack_result(),systemd=systemd_result()))
            after=json.loads(service.render_json())
            self.assertFalse(any(i['capability_id']=='meta.probe_heartbeat' for i in after['incidents']))
            self.assertEqual({i['episode_id'] for i in after['retired_incidents']},{i['episode_id'] for i in old})
            self.assertTrue(all(i['state']=='open' and not i['recovery_evidence'] and i['resolved_at'] is None for i in after['retired_incidents']))
            self.assertEqual(after['retired_incidents'],json.loads(service.render_json())['retired_incidents'])
    def test_only_exact_six_source_keys_are_retired(self):
        class Item:pass
        value=Item()
        for source in r.RETIRED_SOURCES:
            value.incident_key='meta.probe_heartbeat:source_stale:'+_short_hash(source)
            self.assertTrue(r.is_retired(value))
        value.incident_key='meta.probe_heartbeat:source_stale:'+_short_hash('hestia-p0-probe')
        self.assertFalse(r.is_retired(value))
    def test_registry_is_explicit_not_a_suppression_of_all_missing_sources(self):
        root=Path(__file__).parents[3]
        registry=json.loads((root/'Scripts/monitoring/probe-heartbeats.json').read_text())
        entries={s['source']:s for s in registry['sources']}
        for source in r.RETIRED_SOURCES:
            self.assertFalse(entries[source]['enabled']);self.assertIn('retirement_ref',entries[source])
        self.assertTrue(entries['hestia-p0-probe']['enabled'])
    def test_preimages_are_owned_private_idempotent_and_conflicts_preserved(self):
        spec=importlib.util.spec_from_file_location('retire',Path(__file__).parents[2]/'retire_legacy_probes.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);backup=root/'backup';backup.mkdir();unit=root/'old.service';unit.write_text('old bytes')
            result=module.preserve(unit,backup);self.assertIn('before_sha256',result)
            self.assertEqual((backup/'old.service').read_text(),'old bytes')
            self.assertEqual((backup/'old.service').stat().st_mode & 0o777,0o600)
            unit.symlink_to('/dev/null');self.assertEqual(module.preserve(unit,backup)['before'],'already_masked')
            unit.unlink();unit.write_text('different bytes')
            with self.assertRaises(RuntimeError):module.preserve(unit,backup)
            self.assertEqual(unit.read_text(),'different bytes')
    def test_retirement_never_enables_business_tasks_and_default_source_is_modern(self):
        root=Path(__file__).parents[3]
        script=(root/'Stats/retire_legacy_probes.py').read_text()
        self.assertNotIn("'enable'",script);self.assertNotIn('cron-jobs.json',script)
        source=(root/'Stats/exporter/pulse_stats/live.py').read_text()
        self.assertIn('retire_legacy_probes=True',source)
        self.assertIn("snapshot['retired_incidents']",source)

if __name__=='__main__':unittest.main()
