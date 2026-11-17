"""Oikos producer boundary and migration regressions; no production access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).parents[1]))
from pulse_stats import platform_health as h
from pulse_stats import production_paths
from pulse_stats.live_contract import LivePaths

NOW=datetime(2026,9,18,tzinfo=timezone.utc)


def report():
    rows=[]
    for name in sorted(h.NODES):
        rows.append(dict(id=name,label=name,role="resident" if name not in {"C3-ecs","bigbird-sg"} else "runner",
            status="ok",disk_percent=69,memory_available_percent=27,swap_percent=89,
            inode_percent=0,writable="ok",runner_state="not_applicable",runner_enabled="unknown",
            runner_expected="n/a",confirmation_runs=0,delivery="not_needed",reason="",observed_at=NOW.isoformat()))
    return dict(schema=h.SCHEMA,observed_at=NOW.isoformat(),completed_at=NOW.isoformat(),
        expires_at=(NOW+timedelta(seconds=h.MAX_AGE_SECONDS)).isoformat(),source_revision="a"*40,
        source_digest="b"*64,nodes=rows)


class PlatformHealthTests(unittest.TestCase):
    def collect(self,data=None,now=NOW):return h.metrics(json.dumps(data if data is not None else report()),now)

    def test_the_four_registered_nodes_are_required_and_only_sv3_may_join(self):
        data=report();data['nodes'].append(dict(data['nodes'][0],id='unregistered',label='unregistered'))
        self.assertIsNone(self.collect(data)[0][0]['value'])
        data=report();data['nodes']=data['nodes'][1:]
        self.assertIsNone(self.collect(data)[0][0]['value'])
        data=report();data['nodes'].append(dict(data['nodes'][0]))
        self.assertIsNone(self.collect(data)[0][0]['value'])
        self.assertEqual(len(self.collect()[0][0]['value']),4)
        data=report();data['nodes'].append(dict(data['nodes'][0],id='tcloud-node3',label='tcloud-node3',role='resident'))
        metric=self.collect(data)[0][0]
        self.assertEqual((metric['status'],len(metric['value'])),('available',5))
        self.assertIn('3 个常驻节点',metric['reason'])
    def test_private_extra_fields_do_not_enter_app_cache(self):
        data=report();data['nodes'][0]['private_environment']={'secret':'not-for-app'}
        self.assertNotIn('not-for-app',json.dumps(self.collect(data)))

    def test_local_bounded_read_never_executes_or_reaches_another_host(self):
        self.assertEqual(h.REPORT_PATH,Path.home()/'.local/state/oikos/hestia/platform-health-v1.json')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'platform-health-v1.json'
            self.assertIsNone(h.fetch(path))
            path.write_text(json.dumps(report()))
            with patch('subprocess.run',side_effect=AssertionError('no process')):
                self.assertEqual(json.loads(h.fetch(path)),report())
            path.write_bytes(b' '*(h.MAX_BYTES+1))
            self.assertIsNone(h.fetch(path))


    def test_fresh_oikos_still_renders_when_legacy_incident_sources_are_unavailable(self):
        from Stats.exporter.tests.test_live import LiveFixture, FakeRunner, CommandResult
        with tempfile.TemporaryDirectory() as directory:
            fixture=LiveFixture(Path(directory))
            for path in (fixture.registry, fixture.ledger):
                path.unlink(missing_ok=True)
            runner=FakeRunner(ack=CommandResult(1,""),systemd=CommandResult(1,""))
            service=fixture.service(runner,now=NOW,platform_health_fetch=lambda:json.dumps(report()))
            rendered=json.loads(service.render_json())
            metric=next(item for item in rendered['metrics'] if item['id']==h.METRIC_ID)
            self.assertEqual(len(metric['value']),4)
            self.assertEqual(rendered['overall']['runtime_health'],'unknown')
            self.assertIn(metric['quality'],{'validated','descriptive','insufficient','unavailable'})

    def test_unavailable_oikos_does_not_break_valid_legacy_snapshot(self):
        from Stats.exporter.tests.test_live import LiveFixture, FakeRunner, ack_result, systemd_result
        with tempfile.TemporaryDirectory() as directory:
            fixture=LiveFixture(Path(directory))
            service=fixture.service(FakeRunner(ack=ack_result(),systemd=systemd_result()),platform_health_fetch=lambda:None)
            rendered=json.loads(service.render_json())
            metric=next(item for item in rendered['metrics'] if item['id']==h.METRIC_ID)
            self.assertEqual(metric['quality'],'unavailable'); self.assertIsNone(metric['value'])


    def test_unreadable_optional_policy_cannot_blank_platform_or_imply_unowned(self):
        from Stats.exporter.tests.test_live import LiveFixture, FakeRunner, ack_result, systemd_result
        from pulse_stats.usage_sources import _agents_with_stale_sweep
        with tempfile.TemporaryDirectory() as directory:
            fixture=LiveFixture(Path(directory)); original=Path.is_dir
            def guarded(path):
                if path==fixture.paths.policies:raise PermissionError('policy source unavailable')
                return original(path)
            with patch.object(Path,'is_dir',guarded):
                self.assertIsNone(_agents_with_stale_sweep(fixture.paths.policies))
                service=fixture.service(FakeRunner(ack=ack_result(),systemd=systemd_result()),now=NOW,
                                        platform_health_fetch=lambda:json.dumps(report()))
                rendered=json.loads(service.render_json())
                self.assertEqual(len(next(m for m in rendered['metrics'] if m['id']==h.METRIC_ID)['value']),4)

    def test_policy_listing_and_individual_read_failures_are_unknown_not_empty(self):
        from pulse_stats import usage_sources
        directory=Mock(spec=Path); directory.is_dir.return_value=True
        directory.iterdir.side_effect=PermissionError('unavailable listing')
        self.assertIsNone(usage_sources._agents_with_stale_sweep(directory))
        directory.iterdir.side_effect=None
        file=Mock(spec=Path);file.suffix='.yaml';file.is_file.return_value=True
        directory.iterdir.return_value=[file]
        with patch.object(usage_sources,'_load_yaml',return_value=None):
            self.assertIsNone(usage_sources._agents_with_stale_sweep(directory))

    def test_live_fixture_contains_every_filesystem_source_under_its_temp_root(self):
        from dataclasses import fields
        from Stats.exporter.tests.test_live import LiveFixture
        with tempfile.TemporaryDirectory() as directory:
            fixture=LiveFixture(Path(directory))
            for field in fields(fixture.paths):
                value=getattr(fixture.paths,field.name)
                if isinstance(value,Path):
                    self.assertTrue(value.is_relative_to(Path(directory)), field.name)



class RuntimePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.map=self.root/'paths.json'
        self.data={'schema':'pulse_relay_paths/v1','site':'ownermac','root':str(self.root),
            'paths':{'dataDir':str(self.root/'live/data'),'observabilityDir':str(self.root/'live/obs')}}
        self.map.write_text(json.dumps(self.data))

    def test_shared_map_selects_actual_relay_sources_not_stale_copy(self):
        old=self.root/'retired/heartbeats.json';old.parent.mkdir();old.write_text('{}')
        paths,valid=production_paths.resolve(LivePaths(heartbeats=old),self.map)
        self.assertTrue(valid)
        self.assertEqual(paths.heartbeats,self.root/'live/data/heartbeats.json')
        self.assertEqual(paths.obs,self.root/'live/obs')

    def test_invalid_or_missing_mapping_never_falls_back_to_legacy_success(self):
        for key,value in [('schema','old'),('site','wrong'),('root','relative')]:
            data=deepcopy(self.data);data[key]=value;self.map.write_text(json.dumps(data))
            paths,valid=production_paths.resolve(LivePaths(),self.map)
            self.assertFalse(valid);self.assertIn('.unavailable-stats-source',str(paths.heartbeats))
        self.map.unlink();self.assertFalse(production_paths.resolve(LivePaths(),self.map)[1])

    def test_paths_outside_declared_root_are_not_adopted(self):
        self.data['paths']['dataDir']='/tmp/unregistered'
        self.map.write_text(json.dumps(self.data))
        self.assertFalse(production_paths.resolve(LivePaths(),self.map)[1])

    def test_harness_authority_comes_from_the_site_registration(self):
        repository=self.root/'authority'
        registry=self.root/'scl-surfaces.yaml'
        registry.write_text(json.dumps({'surfaces':[
            {'surface_id':'local','deployment':{'site':'ownermac','authority_repository':str(repository)}},
            {'surface_id':'cloud','deployment':{'site':'tcloud','authority_repository':'/elsewhere'}}]}))
        paths,valid=production_paths.resolve(LivePaths(surface_registry=registry),self.map)
        self.assertTrue(valid)
        self.assertEqual(paths.harness_repo,repository)
        self.assertEqual(paths.alerts_dir,repository/'.harness/events')
        self.assertEqual(paths.work_items,repository/'.harness/work-items')
        registry.write_text(json.dumps({'surfaces':[]}))
        paths,valid=production_paths.resolve(LivePaths(surface_registry=registry),self.map)
        # The Relay map stays valid; Harness sources become unreadable, never another checkout.
        self.assertTrue(valid)
        self.assertIn('.unavailable-stats-source',str(paths.work_items))

    def test_installed_reader_is_the_local_launchd_site_without_heartbeat_registry(self):
        root=Path(__file__).parents[3]
        source=(root/'Stats/exporter/pulse_stats/live.py').read_text()
        self.assertIn('registry=None',source)
        self.assertIn('relay_supervisor="launchd"',source)
        self.assertIn('surface_registry=reader/"config/scl-surfaces.yaml"',source)
        self.assertIn('platform_health_fetch=platform_health.fetch',source)


if __name__=='__main__':unittest.main()
