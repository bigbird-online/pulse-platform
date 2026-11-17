"""Monthly failures must start from native data and registered provenance."""
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from test_weekly_native_context import NativeContextTests
from pulse_compute_cost.pi_context import PiAncestry
from pulse_compute_cost.receipts import ReceiptError, canonical


class HostRelocationTests(NativeContextTests):
    def test_new_declared_store_is_read_without_losing_old_provenance(self):
        row=self.user('moved',None,channel='telegram')
        current=self.root/'current';current.mkdir(mode=0o700)
        file=next(self.inputs.glob('*.json'));file.rename(current/file.name)
        source={**self.source,'human_input_directories':[str(self.inputs),str(current)]}
        self.assertEqual(PiAncestry(self.path,source).consume(row).channel,'telegram')
        old=self.user('before-move',None,channel='feishu')
        self.assertEqual(PiAncestry(self.path,source).consume(old).channel,'feishu')

    def test_conflicting_same_input_in_two_registered_stores_fails(self):
        row=self.user('conflict',None)
        current=self.root/'current';current.mkdir(mode=0o700)
        file=next(self.inputs.glob('*.json'));record=json.loads(file.read_bytes())
        record['source']['message']='changed historical message'
        other=current/file.name;other.write_bytes(canonical(record));other.chmod(0o600)
        source={**self.source,'human_input_directories':[str(self.inputs),str(current)]}
        with self.assertRaises(ReceiptError):PiAncestry(self.path,source).consume(row)

    def test_missing_reference_cannot_read_an_unregistered_store(self):
        row=self.user('unregistered',None)
        other=self.root/'unregistered';other.mkdir(mode=0o700)
        file=next(self.inputs.glob('*.json'));file.rename(other/file.name)
        self.assertIsNone(PiAncestry(self.path,self.source).consume(row).channel)


if __name__=='__main__':unittest.main()
