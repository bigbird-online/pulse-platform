"""Oracle Relay Pi sessions resolve their Agent with the TCloud actor wiring."""
from datetime import datetime
from pathlib import Path
import hashlib,json,tempfile,unittest
from pulse_compute_cost.receipts import canonical,reference
from pulse_compute_cost.receipt_sources import collect_registered_sources,HOST_SCHEMA
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.pi_context import tenant_directory
from pulse_compute_cost.collaboration_query import CollaborationQuery,query_collaboration
from test_collaboration_receipts import CATALOG

SITE='oracle-node-1'
SHA='0'*64

class OraclePiActorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.inputs=root/'inputs';self.inputs.mkdir(mode=0o700)
        pool='owner:aki';folder=root/tenant_directory(pool);folder.mkdir()
        self.assertTrue(folder.name.startswith('owner_aki-'))
        self.native=folder/'session.jsonl';self.host=root/'host.jsonl';self.ledger=UsageLedger(root/'ledger')
        start,end='2026-11-10T01:00:01Z','2026-11-10T01:00:04Z'
        values=['telegram','input-one','fixture-human',pool]
        token=hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()
        original={'source':dict(zip(('channel','eventId','humanRef'),values[:3]),message='continue'),'poolKey':pool,
                  'observedAtMs':int(datetime.fromisoformat('2026-11-10T01:00:00+00:00').timestamp()*1000)}
        p=self.inputs/(token+'.json');p.write_bytes(canonical(original));p.chmod(0o600)
        ex,assignment=reference('execution','host'),reference('assignment','one')
        common={'wi':None,'executor':'aki','responsible':None,'delegator':None}
        events=[{'schema':HOST_SCHEMA,'kind':'execution','event_ref':reference('event','begin'),'attempt_ref':ex,'at':start,
                 'runtime':'pi','state':'accepted','expected_usage':True,'assignment_ref':assignment,**common},
          {'schema':HOST_SCHEMA,'kind':'attribution','event_ref':reference('host_event',['human_assignment',ex,reference('human',token)]),
           'at':start,'target_ref':reference('human',token),
           'attribution_ref':reference('attribution',['human_assignment',reference('human',token),assignment]),
           'policy':'authenticated_dispatch_assignment.v1','state':'assigned','effective_at':'2026-11-10T01:00:00Z',**common},
          {'schema':HOST_SCHEMA,'kind':'execution','event_ref':reference('event','end'),'attempt_ref':ex,'at':end,'started_at':start,
           'runtime':'pi','state':'completed','expected_usage':True,'assignment_ref':assignment,**common}]
        records=[{'type':'session','id':'session-one','timestamp':start},
          {'type':'message','id':'input','parentId':'session-one','timestamp':'2026-11-10T01:00:01.010Z',
           'message':{'role':'user','content':'continue\n\n[Host human input reference: '+token+'. provenance only.]'}},
          {'type':'message','id':'answer','parentId':'input','timestamp':'2026-11-10T01:00:03Z',
           'message':{'role':'assistant','model':'openai/test','provider':'fixture','stopReason':'stop',
                      'usage':{'input':3,'output':2,'cacheRead':7,'cacheWrite':0,'totalTokens':12}}}]
        self.host.write_bytes(b'\n'.join(canonical(x) for x in events)+b'\n')
        self.native.write_bytes(b'\n'.join(canonical(x) for x in records)+b'\n')
        # The fields the Oracle source plan gives its Host and Relay Pi sources.
        wiring={'node':SITE,'actor_attribution':True,'host_native_input':True,'human_input_directory':str(self.inputs)}
        self.registry={'schema':'computecost_source_registry/v1','ledger_root':str(self.ledger.root),
          'required_receipt_features':['actor_context.v1','host_native_input.v1'],'sources':[
            {'id':'oracle.relay','kind':'relay_host','path':str(self.host),**wiring},
            {'id':'oracle.pi','kind':'pi','path':str(folder),**wiring}]}
    def history(self,*aliases):
        return [{'alias':a,'principal':a,'effective_from':'2026-11-01T00:00:00Z','config_sha256':SHA} for a in aliases]
    def agents(self):
        collect_registered_sources(self.registry,self.ledger)
        report=query_collaboration(self.ledger,CollaborationQuery(group_by=('agent',)),catalog=CATALOG)
        return {g['key']['agent']:g['usage']['event_count'] for g in report['groups'] if g['usage']['event_count']}
    def test_pi_session_in_its_tenant_directory_resolves_to_its_agent(self):
        self.registry['sources'][0]['principal_alias_history']=self.history('aki','asobi','pulse')
        self.assertEqual(self.agents(),{'aki':1})
    def test_source_plan_moves_to_the_running_host_input_directory(self):
        """The Host state moved: the old input directory is empty, inputs are
        in the running Host's directory. The Oracle plan reads the latter."""
        import importlib.util
        root=Path(__file__).resolve().parents[2]
        spec=importlib.util.spec_from_file_location('stats_deploy_oracle',root/'Stats/deploy.py')
        deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)
        state=Path(self.tmp.name)/'scl-host';state.mkdir(mode=0o700)
        current=state/'human-inputs';self.inputs.rename(current)
        old=Path(self.tmp.name)/'old-inputs';old.mkdir(mode=0o700)
        host,pi=self.registry['sources']
        for source in (host,pi):
            for field in ('actor_attribution','host_native_input','node'):source.pop(field)
            source['human_input_directory']=str(old)
        pi['runtime_manifest']='fixture'  # The Relay Pi source the plan selects.
        surfaces=json.loads((root/'config/scl-surfaces.yaml').read_text())
        deploy.oracle_actor_sources(Path(self.tmp.name),self.registry,surfaces,
                                    {'SCL_HUMAN_DECISION_KEY_FILE':str(state/'human-decision.key')})
        del pi['runtime_manifest']  # This fixture reads the session folder directly.
        host['principal_alias_history']=self.history('aki','pulse')
        self.assertEqual(self.agents(),{'aki':1})
        for source in (host,pi):
            self.assertEqual((source['human_input_directory'],source['human_input_directories']),(str(current),[str(old)]))
    def test_alias_history_without_the_agent_leaves_the_session_unassigned(self):
        self.registry['sources'][0]['principal_alias_history']=self.history('pulse')
        self.assertNotIn('aki',self.agents())

if __name__=='__main__':unittest.main()
