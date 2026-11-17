from copy import deepcopy
import unittest
from Stats.exporter.tests.verify_installed_dashboard import verify_current_activity

class CurrentWindowTests(unittest.TestCase):
    def view(self,period):
        return {'period':period,'dimension':'activity','watermark':7,
            'summary':{'usage':{'event_count':1,'time_unallocated_event_count':0,
            'conflicted_event_count':0,'source_details':[]},'cost':{'unpriced':[],
            'reasons':['wi_execution_inventory_unknown']}},'groups':[{'key':{
            'agent':'hestia','wi':'none','entrypoint':'feishu','node':'ownermac',
            'runtime':'pi','model':'model-from-native'},'usage':{'event_count':1}}]}
    def test_both_windows_pass_without_requiring_inactive_agents(self):
        a,b=self.view('today'),self.view('week'); before=deepcopy([a,b])
        result=verify_current_activity(a,b)
        self.assertEqual(set(result),{'today','week'})
        self.assertEqual([a,b],before)
    def test_each_observed_identity_is_required_independently(self):
        for index in (0,1):
            for column in ('agent','wi','entrypoint','node','runtime','model'):
                for missing in (None,'unassigned','ambiguous','unknown',''):
                    with self.subTest(index=index,column=column,missing=missing):
                        views=[self.view('today'),self.view('week')]
                        views[index]['groups'][0]['key'][column]=missing
                        with self.assertRaisesRegex(AssertionError,views[index]['period']):
                            verify_current_activity(*views)
    def test_reports_both_failed_windows_in_one_response(self):
        a,b=self.view('today'),self.view('week')
        a['groups'][0]['key']['agent']=None;b['groups'][0]['key']['model']=None
        with self.assertRaises(AssertionError) as caught:verify_current_activity(a,b)
        self.assertIn('today',str(caught.exception));self.assertIn('week',str(caught.exception))
    def test_full_unpriced_count_is_not_the_bounded_sample_size(self):
        a,b=self.view('today'),self.view('week')
        a['summary']['cost'].update(unpriced=[{'reason':'missing_price_or_model'}],unpriced_count=526)
        with self.assertRaisesRegex(AssertionError,'526'):verify_current_activity(a,b)
    def test_invalid_or_hidden_unpriced_counts_are_rejected(self):
        for value in (True,-1,1.5,0):
            a,b=self.view('today'),self.view('week')
            a['summary']['cost'].update(unpriced=[{'reason':'missing'}],unpriced_count=value)
            with self.assertRaises(AssertionError):verify_current_activity(a,b)
    def test_window_watermark_or_period_mismatch_is_rejected(self):
        a,b=self.view('today'),self.view('week'); b['watermark']=8
        with self.assertRaises(AssertionError):verify_current_activity(a,b)
        b=self.view('week');a['period']='month'
        with self.assertRaises(AssertionError):verify_current_activity(a,b)
    def test_actual_source_failures_and_lost_rows_fail_either_window(self):
        for index in (0,1):
            for issue in ('source','row','time','conflict'):
                views=[self.view('today'),self.view('week')];v=views[index]
                if issue=='source':v['summary']['usage']['source_details']=[{'status':'unreachable','files_invalid':0}]
                elif issue=='row':v['groups']=[]
                elif issue=='time':v['summary']['usage']['time_unallocated_event_count']=1
                else:v['summary']['usage']['conflicted_event_count']=1
                with self.assertRaises(AssertionError):verify_current_activity(*views)
