"""Read-side timing regressions: no real services used."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from Stats.exporter.tests.test_platform_health import report, NOW
from Stats.exporter.tests.test_live import LiveFixture, FakeRunner, ack_result, systemd_result
from pulse_stats import live

ROOT = Path(__file__).resolve().parents[3]

class ResourceReceiptTimeTests(unittest.TestCase):
    def test_publication_during_collection_is_not_mistaken_for_a_future_clock(self):
        received = NOW + timedelta(seconds=45)
        data = report()
        data['completed_at'] = (NOW+timedelta(seconds=30)).isoformat()
        with tempfile.TemporaryDirectory() as name:
            fixture=LiveFixture(Path(name))
            service=fixture.service(FakeRunner(ack=ack_result(),systemd=systemd_result()),
                now=NOW,live_clock=True,platform_health_fetch=lambda:json.dumps(data))
            with patch.object(live,'datetime',wraps=datetime) as clock:
                clock.now.return_value=received
                result=json.loads(service.render_json())
            metric=next(m for m in result['metrics'] if m['id']=='oikos.platform.nodes')
            self.assertEqual(metric['status'],'available')
            self.assertEqual(metric['observed_at'],NOW.isoformat())
            self.assertEqual(len(metric['value']),4)
            self.assertEqual(metric['value'][0]['expires_at'],data['expires_at'])

    def test_truly_future_publication_remains_unknown(self):
        data=report();data['completed_at']=(NOW+timedelta(minutes=5)).isoformat()
        with tempfile.TemporaryDirectory() as name:
            fixture=LiveFixture(Path(name))
            service=fixture.service(FakeRunner(ack=ack_result(),systemd=systemd_result()),
                now=NOW,live_clock=True,platform_health_fetch=lambda:json.dumps(data))
            with patch.object(live,'datetime',wraps=datetime) as clock:
                clock.now.return_value=NOW+timedelta(seconds=20)
                result=json.loads(service.render_json())
            metric=next(m for m in result['metrics'] if m['id']=='oikos.platform.nodes')
            self.assertEqual(metric['status'],'unknown')
            self.assertIsNone(metric['value'])

if __name__=='__main__':unittest.main()
