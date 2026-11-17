"""Same price content may have a later fetch than the cached query witness."""
import unittest
from pulse_compute_cost.receipts import digest
from Stats.exporter.tests.verify_installed_dashboard import retained_catalog_witness

class RetainedCatalogWitnessTests(unittest.TestCase):
    def test_original_witness_is_used_for_same_content_refetch(self):
        catalog={'catalog':{'data':[]},'retrieved_at':'2026-10-04T09:00:00Z'}
        view={'catalog_sha256':digest(catalog['catalog']),'catalog_observed_at':'2026-10-04T08:00:00Z'}
        self.assertEqual(retained_catalog_witness(view,catalog),view['catalog_observed_at'])
        with self.assertRaises(AssertionError):
            retained_catalog_witness(view,{**catalog,'catalog':{'data':[{'id':'changed'}]}})
        with self.assertRaises(AssertionError):
            retained_catalog_witness(view,{**catalog,'retrieved_at':'2026-10-04T07:00:00Z'})
