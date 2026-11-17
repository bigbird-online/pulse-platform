"""Real-shape parentSession history, including negative and historical cases."""
from pathlib import Path
import json
import tempfile
import unittest
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import read_pi, collect_registered_sources
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.receipts import canonical
from test_collaboration_receipts import AT, CATALOG


def native(identity, amount=10, parent=None):
    return {'type':'message','id':identity,'parentId':parent,'timestamp':AT,
            'message':{'role':'assistant','model':'openai/test','provider':'openai',
                       'usage':{'input':amount,'output':2,'cacheRead':0,'cacheWrite':0}}}


class PiParentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.files=self.root/'sessions';self.files.mkdir()
        self.ledger=UsageLedger(self.root/'ledger')
        self.parent=self.files/'parent.jsonl';self.child=self.files/'child.jsonl'
        self.reg={'schema':'computecost_source_registry/v1','required_receipt_features':['pi_parent_history.v1'],'sources':[{'id':'pi-test','kind':'pi','path':str(self.files)}]}

    def write(self,path,session,rows,parent=None):
        header={'type':'session','id':session,'version':3,'timestamp':AT,'cwd':str(self.root)}
        if parent is not None:header['parentSession']=str(parent)
        path.write_bytes(b''.join(canonical(r)+b'\n' for r in [header,*rows]))

    def query(self,watermark=None):
        return query_collaboration(self.ledger,CollaborationQuery(watermark=watermark),catalog=CATALOG,catalog_observed_at=AT)

    def test_parent_copy_is_not_a_second_charge_and_history_remains_readable(self):
        self.write(self.parent,'parent',[native('a')])
        self.write(self.child,'child',[native('a'),native('b',30,'a')],self.parent)
        self.ledger.ingest_receipts(read_pi(self.parent,AT)+read_pi(self.child,AT))
        mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        self.assertEqual(before['summary']['usage']['known_total_tokens'],56)
        collect_registered_sources(self.reg,self.ledger)
        after=self.query()
        self.assertEqual(after['summary']['usage']['known_total_tokens'],44)
        self.assertEqual(after['summary']['usage']['event_count'],2)
        self.assertEqual(len(after['reconciliation']['copied_native_observations']),1)
        self.assertEqual(self.query(mark)['report_sha256'],before['report_sha256'])
        self.assertEqual(collect_registered_sources(self.reg,self.ledger)['inserted'],0)

    def test_incremental_source_prefix_refinement_matches_unindexed_logic(self):
        from pulse_compute_cost.receipt_sources import _revision
        self.write(self.parent,'parent',[native('a'),native('b',30,'a')])
        incoming=read_pi(self.parent,AT)
        simple=[];indexed=[];histories={}
        for row in incoming+incoming:
            _revision(simple,row)
            _revision(indexed,row,histories=histories)
        self.assertEqual([r.to_dict() for r in indexed],[r.to_dict() for r in simple])
        self.assertEqual(len(indexed),2)

    def test_identical_rows_without_parent_relationship_are_not_deduplicated(self):
        self.write(self.parent,'parent',[native('same')])
        self.write(self.child,'child',[native('same')])
        collect_registered_sources(self.reg,self.ledger)
        self.assertEqual(self.query()['summary']['usage']['known_total_tokens'],24)

    def test_writer_waits_for_existing_reader_feature_activation(self):
        self.write(self.parent,'parent',[native('a')])
        self.write(self.child,'child',[native('a')],self.parent)
        del self.reg['required_receipt_features']
        collect_registered_sources(self.reg,self.ledger)
        self.assertFalse(any(r.producer=='pi_parent_history.v1' for r in self.ledger.receipt_snapshot()['facts']))
        self.assertEqual(self.query()['summary']['usage']['known_total_tokens'],24)

    def test_missing_original_is_never_discarded(self):
        self.write(self.child,'child',[native('a')],self.parent)
        collect_registered_sources(self.reg,self.ledger)
        self.assertEqual(self.query()['summary']['usage']['known_total_tokens'],12)

    def test_changed_history_is_a_source_error_not_silently_removed(self):
        self.write(self.parent,'parent',[native('a')])
        self.write(self.child,'child',[native('a',99)],self.parent)
        result=collect_registered_sources(self.reg,self.ledger)
        self.assertEqual(result['sources'][0]['status'],'partial')
        self.assertEqual(result['sources'][0]['reason'],'pi_parent_history_invalid')
        self.assertEqual(self.query()['summary']['usage']['event_count'],2)

    def test_three_generations_keep_only_the_original_consumption(self):
        grandchild=self.files/'grandchild.jsonl'
        self.write(self.parent,'parent',[native('a')])
        self.write(self.child,'child',[native('a')],self.parent)
        self.write(grandchild,'grandchild',[native('a')],self.child)
        collect_registered_sources(self.reg,self.ledger)
        self.assertEqual(self.query()['summary']['usage']['known_total_tokens'],12)

    def test_cyclic_parent_evidence_never_selects_a_winner(self):
        self.write(self.parent,'parent',[native('a')],self.child)
        self.write(self.child,'child',[native('a')],self.parent)
        result=collect_registered_sources(self.reg,self.ledger)
        self.assertEqual(result['sources'][0]['status'],'partial')
        self.assertEqual(self.query()['summary']['usage']['event_count'],2)


if __name__=='__main__':unittest.main()
