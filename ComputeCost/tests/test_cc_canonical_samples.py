"""The bounded CC snapshot consumes the already-reviewed canonical sample builder."""
import json
from unittest import mock
import pytest
from pulse_compute_cost import runtime_samples
from pulse_compute_cost.cc_usage import CcUsageBinding, CcSource, cc_authority_ref, collect_cc_session_usage

@pytest.mark.parametrize('runtime', ['cc', 'cc_sdk'])
def test_bounded_cc_uses_canonical_samples_and_original_binding(runtime):
    session = 'native-session'
    binding = CcUsageBinding(work_item_id='wi_20261001_297',agent_id='pulse',phase='implementation',epoch=2,
        runtime=runtime,procurement_source='claude_subscription',authority_ref=cc_authority_ref(session),
        execution_surface='surface_v1_ownermac_relay',runtime_binding_sha256='c'*64)
    row = {'type':'assistant','uuid':'entry','sessionId':session,'timestamp':'2026-10-03T00:00:01Z',
        'message':{'id':'message','model':'claude-opus-4-1','usage':{'input_tokens':10,'output_tokens':8,
        'cache_read_input_tokens':50,'cache_creation_input_tokens':4}}}
    raw=(json.dumps(row)+'\n').encode()
    original=runtime_samples.build_cc_usage_sample
    samples=[]
    def capture(*args,**kwargs):
        sample=original(*args,**kwargs);samples.append(sample);return sample
    with mock.patch.object(runtime_samples,'build_cc_usage_sample',side_effect=capture):
        result=collect_cc_session_usage([CcSource('main',raw,0)],session_id=session,binding=binding)
    assert len(samples)==1
    sample=samples[0];runtime_samples.validate_runtime_usage_sample(sample)
    assert sample['runtime_binding_sha256']=='c'*64
    assert sample['tokens']=={'input':64,'output':8,'cache_read':50,'cache_write':4,'reasoning':None,'total':72}
    summary=result[0].summary()
    assert summary['totals']=={'input':10,'output':8,'cache_read':50,'cache_write':4,'reasoning':0,'total_tokens':72}
