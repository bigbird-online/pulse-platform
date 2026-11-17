"""A profile without a Host is not a conflicting Host-kernel declaration."""
from pathlib import Path
import json
import tempfile
import unittest
from pulse_compute_cost.source_layout import native_binding_source


class NativeManifestBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manifest=self.root/'runtime.json'
        self.profile={'sessionRoot':str(self.root/'sessions')}
        self.managed={**self.profile,'computeCostHost':{'sclKernelRoot':str(self.root/'kernel')}}
        self.source={'id':'fixture','kind':'pi','runtime_manifest':str(self.manifest),
                     'profiles':['unbound','managed'],'scl_surface_id':'surface_v1_cloud_linux_a'}

    def write(self, unbound=None, managed=None):
        self.manifest.write_text(json.dumps({'profiles':{
            'unbound': self.profile if unbound is None else unbound,
            'managed': self.managed if managed is None else managed}}))

    def test_unbound_profile_does_not_disable_the_enrolled_kernel(self):
        self.write()
        self.assertEqual(native_binding_source(self.source)['scl_kernel_root'],str(self.root/'kernel'))

    def test_two_different_declared_kernels_still_fail(self):
        self.write(unbound={**self.profile,'computeCostHost':{'sclKernelRoot':str(self.root/'other')}})
        with self.assertRaises(ValueError):native_binding_source(self.source)

    def test_malformed_host_is_not_treated_as_an_unbound_profile(self):
        for host in ({}, {'sclKernelRoot':None}, 'invalid'):
            with self.subTest(host=host):
                self.write(unbound={**self.profile,'computeCostHost':host})
                with self.assertRaises(ValueError):native_binding_source(self.source)

    def test_all_unbound_profiles_have_no_kernel_instead_of_guessing(self):
        self.write(managed=self.profile)
        self.assertIsNone(native_binding_source(self.source))


if __name__=='__main__':unittest.main()
