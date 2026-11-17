"""Native protocol fixtures only; actor extraction never changes raw usage facts."""
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.receipt_sources import read_codex
from pulse_compute_cost.receipts import reference

AT='2026-09-15T01:00:00Z'; END='2026-09-15T01:01:00Z'


class CodexStartupActorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source={'node':'ownermac','workspace_actors':[{
            'workspace_root':'/registered/PULSE/local-pulse-space','agent':'pulse',
            'node':'ownermac','runtime':'codex','registry_ref':reference('registry','fixture')}]}

    def records(self):
        return [
            {'type':'session_meta','timestamp':AT,'payload':{
                'id':'session-fixture','cwd':'/registered/PULSE/local-pulse-space',
                'originator':'Codex Desktop','source':'vscode'}},
            {'type':'event_msg','timestamp':AT,'payload':{'type':'task_started','turn_id':'one'}},
            {'type':'turn_context','timestamp':AT,'payload':{
                'cwd':'/project/worktree/another-agent-name','model':'gpt-6-astra'}},
            {'type':'event_msg','timestamp':END,'payload':{'type':'token_count','info':{
                'total_token_usage':{'input_tokens':120,'cached_input_tokens':20,
                    'cache_write_input_tokens':0,'output_tokens':10,'reasoning_output_tokens':0}}}},
            {'type':'event_msg','timestamp':END,'payload':{'type':'task_complete'}},
        ]

    def write(self, rows, folder='active', filename='session-fixture.jsonl'):
        path=self.root/folder/filename;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows));path.chmod(0o600)
        return path

    def test_real_parser_keeps_startup_actor_after_project_cwd_changes(self):
        path=self.write(self.records()); before=path.read_bytes()
        plain=read_codex(path.parent,END)
        enriched=read_codex(path.parent,END,source_identity=self.source)
        usage=[r for r in enriched if r.kind=='usage']
        actors=[r for r in enriched if r.kind=='attribution']
        self.assertEqual([r.to_dict() for r in usage],[r.to_dict() for r in plain])
        self.assertEqual(len(usage),1); self.assertEqual(len(actors),1)
        self.assertEqual(actors[0].payload['executor'],'pulse')
        self.assertIsNone(actors[0].payload['wi'])
        self.assertEqual(actors[0].payload['context']['entrypoint'],'codex_app')
        self.assertEqual(usage[0].payload['model'],'gpt-6-astra')
        self.assertEqual(usage[0].payload['tokens'],{
            'uncached_input':100,'cache_read':20,'cache_write':0,'output':10})
        self.assertNotIn('/registered',json.dumps(actors[0].to_dict()))
        self.assertEqual(path.read_bytes(),before)

    def test_active_archive_copies_keep_one_usage_and_one_actor(self):
        rows=self.records(); first=self.write(rows); second=self.write(rows,folder='archive')
        result=read_codex([first.parent,second.parent],END,source_identity=self.source)
        self.assertEqual(sum(r.kind=='usage' for r in result),1)
        self.assertEqual(sum(r.kind=='attribution' for r in result),1)

    def test_unregistered_startup_does_not_inherit_a_later_matching_cwd(self):
        rows=self.records();rows[0]['payload']['cwd']='/other/local-pulse-space'
        rows[2]['payload']['cwd']='/registered/PULSE/local-pulse-space'
        result=read_codex(self.write(rows).parent,END,source_identity=self.source)
        actor=next(r for r in result if r.kind=='attribution')
        self.assertIsNone(actor.payload['executor'])
        self.assertEqual(actor.payload['context']['node'],'ownermac')
        self.assertEqual(actor.payload['context']['entrypoint'],'codex_app')

    def test_conflicting_native_startup_metadata_does_not_pick_last_identity(self):
        rows=self.records()
        rows.insert(1,{'type':'session_meta','timestamp':AT,'payload':{
            **rows[0]['payload'],'cwd':'/conflicting/agent-workspace'}})
        result=read_codex(self.write(rows).parent,END,source_identity=self.source)
        actor=next(r for r in result if r.kind=='attribution')
        self.assertIsNone(actor.payload['executor'])
        self.assertIsNone(actor.payload['context']['entrypoint'])


if __name__=='__main__':unittest.main()
