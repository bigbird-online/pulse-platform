"""An old summary cannot quarantine a different dispatch in the same session."""
import unittest
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.receipts import reference
from pulse_compute_cost.collaboration_query import reconcile_usage

class HostIntervalTest(unittest.TestCase):
    def test_conflict_does_not_extend_outside_explicit_host_interval(self):
        scope=reference('native_session',['pi','same-session'])
        at='2026-09-14T12:00:00Z'
        native={'input':100,'output':10,'cacheRead':20,'cacheWrite':0,'totalTokens':130}
        old=usage_receipt('pi','old',native,at='2026-09-13T01:00:00Z',observed=at,
            model='openai/test',mapping='pi_native.v1',native_scope_ref=scope)
        new=usage_receipt('pi','new',native,at=at,observed=at,
            model='openai/test',mapping='pi_native.v1',native_scope_ref=scope)
        host=usage_receipt('pi','host',{'input_tokens':999,'output_tokens':10,
            'cache_read_input_tokens':20,'cache_creation_input_tokens':0},
            at='2026-09-13T01:01:00Z',observed=at,model=None,mapping='relay_pi.v1',
            native_scope_ref=scope,granularity='turn_aggregate',
            interval=('2026-09-13T00:59:00Z','2026-09-13T01:01:00Z'))
        kept,report=reconcile_usage([old,new,host])
        self.assertEqual([r.entity_ref for r in kept],[new.entity_ref])
        self.assertEqual(set(report['conflicting_refs']),{old.entity_ref,host.entity_ref})

if __name__=='__main__':unittest.main()
