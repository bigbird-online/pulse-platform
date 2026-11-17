"""Independent recovery must have a real dispatched Host journal, not guessed ancestry."""
import json
import hashlib
import unittest
from copy import deepcopy
from pulse_compute_cost.pi_context import PiAncestry, _RECOVERY_PREFIX
from pulse_compute_cost.native_parse_cache import input_signature
from pulse_compute_cost.receipt_sources import read_pi
from pulse_compute_cost.receipts import canonical, ReceiptError
from test_weekly_native_context import NativeContextTests
from test_collaboration_receipts import AT

class DispatchedRecoveryTests(unittest.TestCase):
    setUp = NativeContextTests.setUp
    user = NativeContextTests.user

    def fixture(self):
        self.inputs.rename(self.root / 'human-inputs')
        self.inputs = self.root / 'human-inputs'
        self.source['human_input_directory'] = str(self.inputs)
        original = self.user('independent', None)
        token = original['message']['content'].split('[Host human input reference: ')[1].split('.')[0]
        wi = 'wi_20260831_013'; session = 'native-session-fixture'
        request = 'operation_v1_feishu_recovery_' + hashlib.sha256(json.dumps(
            [self.pool, wi, token], separators=(',', ':')).encode()).hexdigest()
        context = {'machine': {'work_item_id': wi, 'status': 'degraded'}}
        record = {'schema':'pulse_feishu_host_recovery/v1','requestId':request,'poolKey':self.pool,
            'workItemId':wi,'humanInputRef':token,'sessionId':session,'state':'dispatched',
            'dispatching':True,'result':{'lifecycleContext':context}}
        directory = self.root / 'feishu-recoveries'; directory.mkdir(mode=0o700)
        journal = directory / (request + '.json')
        journal.write_bytes(canonical(record)); journal.chmod(0o600)
        header = {'type':'session','id':session,'timestamp':AT}
        row = {'type':'message','id':'recovery','parentId':session,'timestamp':AT,
            'message':{'role':'user','content':_RECOVERY_PREFIX+'\nhuman_input_ref='+token+'\n'
                +json.dumps(context)+'\nSCL_HOST_RECOVERY_REQUEST='+request}}
        return header, row, journal, record

    def consume(self, header, row):
        graph = PiAncestry(self.path, self.source); graph.consume(header)
        return graph.consume(row)

    def test_independent_dispatched_input_proves_channel_without_old_ancestor(self):
        header, row, journal, record = self.fixture()
        context = self.consume(header, row)
        self.assertEqual(context.channel, 'feishu')
        self.assertEqual(context.host_input_token, record['humanInputRef'])
        self.assertIsNotNone(context.input_ref)
        self.assertFalse(hasattr(context, 'execution_authority'))

    def test_missing_or_changed_journal_never_guesses_channel(self):
        header, row, journal, record = self.fixture()
        changes = [('sessionId','another'),('poolKey','other:pulse'),('state','pending'),
                   ('dispatching',False),('workItemId','wi_20260831_014'),
                   ('requestId','operation_v1_feishu_recovery_'+'0'*64)]
        for key, value in changes:
            with self.subTest(key=key):
                journal.write_bytes(canonical({**record,key:value}))
                self.assertIsNone(self.consume(header,row).channel)
        journal.unlink()
        self.assertIsNone(self.consume(header,row).channel)

    def test_copied_or_changed_context_is_not_the_dispatched_result(self):
        header, row, journal, record = self.fixture()
        row['message']['content'] = row['message']['content'].replace('degraded', 'modified')
        self.assertIsNone(self.consume(header,row).channel)

    def test_cache_tracks_arrival_changes_removal_and_rejects_unsafe_journal(self):
        header, row, journal, record = self.fixture()
        first = input_signature(self.source)
        journal.write_bytes(canonical({**record, 'state':'pending'}))
        self.assertNotEqual(first, input_signature(self.source))
        journal.chmod(0o644)
        with self.assertRaises(ValueError): input_signature(self.source)
        with self.assertRaises(ReceiptError): self.consume(header,row)
        journal.unlink(); self.assertNotEqual(first, input_signature(self.source))

    def test_reader_keeps_native_counts_and_uses_verified_session_header(self):
        header, row, journal, record = self.fixture()
        header['cwd'] = str(self.root)
        answer = {'type':'message','id':'answer','parentId':row['id'],'timestamp':AT,
            'message':{'role':'assistant','model':'gpt-6-sol','provider':'openai-codex',
            'usage':{'input':11,'output':3,'cacheRead':7,'cacheWrite':0}}}
        data = b'\n'.join(canonical(r) for r in [header,row,answer]) + b'\n'
        result = read_pi(self.path,AT,data=data,source_identity=self.source)
        usage = [r for r in result if r.kind=='usage']; self.assertEqual(len(usage),1)
        self.assertEqual(usage[0].payload['tokens'],{'uncached_input':11,'output':3,'cache_read':7,'cache_write':0})
        actors = [r for r in result if r.kind=='attribution']
        self.assertEqual(len(actors),1)
        self.assertEqual(actors[0].payload['context']['entrypoint'],'feishu')
        journal.unlink()
        unknown = read_pi(self.path,AT,data=data,source_identity=self.source)
        self.assertEqual([r.to_dict() for r in unknown if r.kind=='usage'],[r.to_dict() for r in usage])
        self.assertFalse(any(r.kind=='attribution' for r in unknown))
