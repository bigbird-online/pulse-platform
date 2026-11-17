"""Real disposable payload/adoption/FD gates; no production effects or network."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
from Stats import publisher as p, publisher_retention as r
from Stats.exporter.tests.test_stats_deploy import d, A, B

C='c'*40


def archive_fixture():
    stream=io.BytesIO();root=Path(d.__file__).parent
    files={'Scripts/pulse-stats-cost-refresh.py':b'print("fixture")\n',
           'Stats/publisher.py':Path(p.__file__).read_bytes(),
           'Stats/publisher_retention.py':Path(r.__file__).read_bytes(),
           'Stats/deploy.py':Path(d.__file__).read_bytes()}
    with tarfile.open(fileobj=stream,mode='w') as archive:
        for name,body in files.items():
            member=tarfile.TarInfo(name);member.size=len(body);member.mode=0o644
            archive.addfile(member,io.BytesIO(body))
    return stream.getvalue()


class ProspectivePublisherRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve();self.state=self.home/'.local/state/pulse/stats'
        self.state.mkdir(mode=0o700,parents=True);self.pool=self.home/'.local/share/pulse/stats/publisher-releases'
        self.registry=self.home/'.local/state/pulse/compute-cost/sources.json';self.registry.parent.mkdir(parents=True)
        d.atomic(self.registry,b'{"sources":[]}')
        self.config=self.state/'refresh.json';self.job=self.home/'Library/LaunchAgents'/f'{d.LABEL}.plist'
        self.job.parent.mkdir(parents=True);self.records={}
        self.data=archive_fixture();self.shared=d.stage_runtime(self.data,self.home,A)
        self.current=self.shared.parent.parent/'current';self.current.symlink_to(self.shared)
        for revision,previous in ((A,'d'*40),(B,A),(C,B)):
            runtime=d.stage_runtime(self.data,self.home,revision,publisher=True)
            config={'source_revision':revision,'peers':[{'id':'tcloud'}],'destination':{'python':'keep','host':'keep'}}
            d.atomic(self.config,json.dumps(config).encode());args=d.publisher_arguments(self.home,runtime,install=True)
            d.atomic(self.job,plistlib.dumps({'Label':d.LABEL,'StartInterval':1800,'ProgramArguments':args}))
            attempt=self.state/('refresh-local-'+revision);attempt.mkdir(mode=0o700)
            receipt=attempt/'receipt.json';d.atomic(receipt,json.dumps({'scope':'local_cost_refresh',
                'revision':revision,'previous':previous,'status':'installed','publisher_policy':r.POLICY,
                'completed_at':'2026-01-01T00:00:00+00:00'}).encode())
            final=receipt.read_bytes();selected={**json.loads(final),'status':'publisher_payload_selected',
                 'selected_at':'2026-01-01T00:00:00+00:00'}
            d.atomic(receipt,json.dumps(selected).encode());d.record_retention_adoption(self.home,receipt,enrollment=True)
            d.atomic(receipt,final);d.record_retention_adoption(self.home,receipt)
        self.records=r.adoptions(self.home)
        self.signature=r.input_signature(self.home,json.loads(self.config.read_bytes()))
        for revision in (B,C):self.prove(revision)

    def prove(self,revision,*,gap=1801,bad_destination=False):
        record,digest=self.records[revision];directory=self.state/'publisher-publications'
        directory.mkdir(mode=0o700,exist_ok=True)
        for count,stamp in enumerate((1767225601,1767225601+gap),1):
            report={'generated_at':datetime.fromtimestamp(stamp,timezone.utc).isoformat(),'source_revision':revision,
                'peers':[{'id':'tcloud','status':'read'}],
                'codex_quota':'published','cc_quota':'published','destination':'published'}
            body=json.dumps(report);sha=hashlib.sha256(body.encode()).hexdigest()
            proof={'schema':'pulse_stats_publisher_publication/v1','revision':revision,'adoption':digest,
                'report_sha256':sha,'report_body':body,'completed_at':stamp,'run_count':count,
                'started_at':stamp,'destination_identity':r.destination_identity(json.loads(self.config.read_bytes())),
                'entry_sha256':record['publisher']['entry_sha256'],'input_signature':self.signature,
                'destination':{'bytes':128,'sha256':'a'*64,'generated_at':report['generated_at']}}
            if bad_destination:proof.pop('destination')
            r.once(directory/digest/(sha+'.json'),proof)

    @contextmanager
    def locks(self):
        fds=[]
        try:
            for name in ('deployment.lock','cost-refresh.lock'):
                fd=os.open(self.state/name,os.O_RDWR|os.O_CREAT,0o600);fds.append(fd)
                r.held_lock(self.home,name,fd)
            yield fds
        finally:
            for fd in reversed(fds):os.close(fd)

    def retire(self,reference=None):
        with self.locks() as fds,patch.object(r,'reference_snapshot',return_value=reference or {'config':'same'}):
            return r.retire(self.home,C,self.records,*fds)

    def test_real_three_adoptions_remove_only_exclusive_a_preserve_shared_and_rollback(self):
        before=self.current.lstat().st_ino;result=self.retire()
        self.assertEqual(result['status'],'completed');self.assertGreater(result['allocated_bytes'],0)
        self.assertFalse((self.pool/A).exists());self.assertTrue(self.shared.exists())
        self.assertTrue((self.pool/B).exists());self.assertTrue((self.pool/C).exists())
        self.assertEqual(self.current.lstat().st_ino,before);self.assertEqual(list((self.pool/'.retiring').iterdir()),[])
        self.assertEqual(len(list((self.state/'retention-adoptions').iterdir())),3)
        self.assertEqual(len(list((self.state/'publisher-retirements').glob('*completed.json'))),1)

    def test_missing_rollback_acceptance_holds_every_payload(self):
        record,digest=self.records[B]
        import shutil
        shutil.rmtree(self.state/'publisher-publications'/digest)
        self.assertEqual(self.retire()['status'],'protected');self.assertTrue((self.pool/A).exists())

    def test_short_spacing_is_not_a_later_natural_acceptance(self):
        record,digest=self.records[B]
        import shutil
        shutil.rmtree(self.state/'publisher-publications'/digest);self.prove(B,gap=2)
        self.assertFalse(r.accepted(self.home,record,digest,self.signature));self.assertEqual(self.retire()['status'],'protected')

    def test_missing_destination_or_corrupt_report_cannot_accept(self):
        record,digest=self.records[B];directory=self.state/'publisher-publications'/digest
        for malformed in ('destination','body','sha','period'):
            with self.subTest(malformed=malformed):
                path=next(directory.iterdir());original=path.read_bytes();proof=json.loads(original)
                if malformed=='destination':proof.pop('destination')
                elif malformed=='body':proof['report_body']='{}'
                elif malformed=='sha':proof['report_sha256']='0'
                else:proof['run_count']='2'
                d.atomic(path,json.dumps(proof).encode())
                with self.assertRaises((ValueError,KeyError,TypeError)):r.accepted(self.home,record,digest,self.signature)
                d.atomic(path,original)

    def test_new_configuration_or_registry_cannot_use_prior_acceptance(self):
        d.atomic(self.registry,b'{"sources":[],"changed":true}')
        self.assertEqual(self.retire()['status'],'protected');self.assertTrue((self.pool/A).exists())

    def test_destination_forward_holds_prior_proofs_without_corruption_error(self):
        old_signature=self.signature
        config=json.loads(self.config.read_bytes());config['destination']['host']='new-formal-host'
        d.atomic(self.config,json.dumps(config).encode())
        signature=r.input_signature(self.home,config)
        self.assertNotEqual(signature,old_signature)
        record,digest=self.records[B]
        self.assertFalse(r.accepted(self.home,record,digest,signature))
        self.assertEqual(self.retire(),{'status':'protected','reason':'current_not_accepted'})
        self.assertTrue(all((self.pool/revision).exists() for revision in (A,B,C)))
        self.assertFalse((self.state/'publisher-retirements').exists())

    def test_incompatible_proof_still_rejects_corrupt_report_or_destination_shape(self):
        record,digest=self.records[B];path=next((self.state/'publisher-publications'/digest).iterdir())
        original=path.read_bytes()
        for mutation in ('body','destination'):
            with self.subTest(mutation=mutation):
                proof=json.loads(original);proof['input_signature']='b'*64
                if mutation=='body':proof['report_body']='{}'
                else:proof['destination_identity']='invalid'
                d.atomic(path,json.dumps(proof).encode())
                with self.assertRaises(ValueError):r.accepted(self.home,record,digest,self.signature)
                d.atomic(path,original)

    def test_same_input_signature_still_rejects_wrong_destination(self):
        record,digest=self.records[B];path=next((self.state/'publisher-publications'/digest).iterdir())
        proof=json.loads(path.read_bytes());proof['destination_identity']='c'*64
        d.atomic(path,json.dumps(proof).encode())
        with self.assertRaisesRegex(ValueError,'publication destination differs'):
            r.accepted(self.home,record,digest,self.signature)

    def test_valid_compatible_pair_is_not_blocked_by_incompatible_history(self):
        record,digest=self.records[C];directory=self.state/'publisher-publications'/digest
        proof=json.loads(next(directory.iterdir()).read_bytes());stamp=proof['started_at']+900
        report=json.loads(proof['report_body']);report['generated_at']=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        body=json.dumps(report);sha=hashlib.sha256(body.encode()).hexdigest()
        proof.update(report_body=body,report_sha256=sha,completed_at=stamp,started_at=stamp,
                     input_signature='b'*64,destination_identity='c'*64)
        proof['destination']['generated_at']=report['generated_at']
        r.once(directory/(sha+'.json'),proof)
        self.assertTrue(r.accepted(self.home,record,digest,self.signature))

    def test_symlink_hardlink_unknown_or_mutated_payload_is_held(self):
        target=self.pool/A/'Scripts/pulse-stats-cost-refresh.py';body=target.read_bytes()
        for mutation in ('symlink','hardlink','unknown','bytes'):
            with self.subTest(mutation=mutation):
                other=self.home/'other';unknown=self.pool/A/'unknown'
                if mutation=='symlink':target.unlink();target.symlink_to(self.shared/'Scripts/pulse-stats-cost-refresh.py')
                elif mutation=='hardlink':os.link(target,other)
                elif mutation=='unknown':unknown.write_text('not disposable')
                else:target.write_bytes(b'altered')
                with self.assertRaises(ValueError):self.retire()
                self.assertTrue((self.pool/A).exists())
                if mutation=='hardlink':other.unlink()
                elif mutation=='unknown':unknown.unlink()
                else:target.unlink();target.write_bytes(body);target.chmod(0o644)

    def test_replacement_staged_inode_is_never_deleted(self):
        original=r.remove_payload;other=self.pool/'.retiring'/'.original-held'
        def replace(stage,revision,record):
            (stage/revision).rename(other);(stage/revision).mkdir(mode=0o700)
            (stage/revision/'valuable').write_text('retain')
            original(stage,revision,record)
        with patch.object(r,'remove_payload',side_effect=replace),self.assertRaises(ValueError):self.retire()
        self.assertEqual((self.pool/'.retiring'/A/'valuable').read_text(),'retain')
        self.assertTrue(other.exists());self.assertEqual(len(list((self.state/'publisher-retirements').glob('*failed.json'))),1)

    def test_source_or_original_manifest_changed_rejected_before_loading(self):
        runtime=self.pool/C;manifest=runtime/'.stats-files.json';original=manifest.read_bytes()
        d.atomic(manifest,b'{}')
        with patch.object(p.runpy,'run_path') as load,self.assertRaises(ValueError):p.publish(self.home,self.config)
        load.assert_not_called();d.atomic(manifest,original)

    def test_generic_relative_pool_or_revision_reference_holds(self):
        settings=self.home/'.claude/settings.json';settings.parent.mkdir()
        for body in (b'{"script":"publisher-releases/$REV/consumer.py"}',json.dumps({'revision':A}).encode()):
            d.atomic(settings,body)
            with self.assertRaises(ValueError):r.reference_snapshot(self.home,self.pool/A)

    def test_first_success_without_matching_loaded_identity_cannot_accept(self):
        revision=C;runtime=self.pool/C;record,digest=self.records[C]
        now=time.time();report={'generated_at':datetime.now(timezone.utc).isoformat(),'source_revision':C,
            'peers':[{'id':'tcloud','status':'read'}],
            'codex_quota':'published','cc_quota':'published','destination':'published'}
        d.atomic(self.state/'last-refresh.json',json.dumps(report).encode())
        with self.locks() as fds,patch.object(r,'loaded',return_value=(r.expected_arguments(self.home,record),os.getpid()+1,3)),patch.object(r,'once') as write:
            result=r.after_publication(self.home,runtime,self.config,now-1,fds[0])
        self.assertEqual(result['status'],'failed');self.assertTrue(all('publication' not in str(call.args[0]) for call in write.call_args_list))

    def test_unregistered_config_or_cadence_change_cannot_accept(self):
        for change in ('config','period'):
            with self.subTest(change=change):
                before_config=self.config.read_bytes();before_job=self.job.read_bytes()
                if change=='config':value=json.loads(before_config);value['codex_quota']={'command':['foreign']};d.atomic(self.config,json.dumps(value).encode())
                else:value=plistlib.loads(before_job);value['StartInterval']=5;d.atomic(self.job,plistlib.dumps(value))
                with self.locks() as fds,patch.object(r,'once') as write:
                    result=r.after_publication(self.home,self.pool/C,self.config,time.time(),fds[0])
                self.assertEqual(result['status'],'failed');self.assertTrue(all('publication' not in str(call.args[0]) for call in write.call_args_list))
                d.atomic(self.config,before_config);d.atomic(self.job,before_job)

    def test_business_completion_follows_registered_peers_and_optional_destination(self):
        report={'source_revision':C,'peers':[{'id':'tcloud','status':'read'}],
                'codex_quota':'published','cc_quota':'published','destination':'not_configured'}
        local={'source_revision':C,'peers':[{'id':'tcloud'}]}
        self.assertTrue(r.complete_business(report,C,local))
        self.assertFalse(r.complete_business(report,C,{**local,'destination':{'host':'h'}}))
        self.assertFalse(r.complete_business(report,C,{**local,'peers':[{'id':'tcloud'},{'id':'other'}]}))
        self.assertFalse(r.complete_business({**report,'peers':[{'id':'tcloud','status':'unreachable_or_invalid'}]},C,local))
        self.assertFalse(r.complete_business({**report,'peers':[]},C,{**local,'peers':[]}))
        # A peer paused by decision is complete as reported, never as an unread failure.
        paused={**report,'peers':[{'id':'tcloud','status':'paused','reason':'paused_by_decision'},
                                  {'id':'oracle-node-1','status':'read'}]}
        both={**local,'peers':[{'id':'tcloud'},{'id':'oracle-node-1'}]}
        self.assertTrue(r.complete_business(paused,C,both))
        self.assertFalse(r.complete_business({**paused,'peers':[{'id':'tcloud','status':'paused','reason':'other'}]},C,local))
        # A historical proof is intrinsically valid without today's peer list.
        retired={**report,'peers':[{'id':'vultr','status':'read'},{'id':'tcloud','status':'read'}],'destination':'published'}
        self.assertTrue(r.complete_business(retired,C))

    def test_local_only_readback_needs_no_remote_host(self):
        dashboard=self.state/'cost-dashboard-v1.json'
        d.atomic(dashboard,json.dumps({'generated_at':'2026-01-01T00:00:00+00:00','source_revision':C}).encode())
        with patch.object(r.subprocess,'run',side_effect=AssertionError('no remote readback')):
            proof=r.destination_readback(self.home,{'source_revision':C,'peers':[]})
        self.assertEqual(proof['bytes'],len(dashboard.read_bytes()))
        with self.assertRaises(ValueError):
            r.destination_readback(self.home,{'source_revision':B,'peers':[]})
        self.assertNotEqual(r.destination_identity({'peers':[]}),r.destination_identity({'destination':{'host':'h'}}))

    def test_lock_identity_replacement_refuses_retirement(self):
        with self.locks() as fds:
            path=self.state/'deployment.lock';path.rename(self.state/'old-lock');path.write_bytes(b'');path.chmod(0o600)
            with self.assertRaises(ValueError):r.retire(self.home,C,self.records,*fds)
        self.assertTrue((self.pool/A).exists())

    def test_malformed_destination_generation_and_nonobject_proof_hold(self):
        _,digest=self.records[B];directory=self.state/'publisher-publications'/digest
        first=next(directory.iterdir());body=first.read_bytes();proof=json.loads(body)
        for value in ([],{**proof,'destination':{**proof['destination'],'generated_at':'not-a-date'}},
                      {**proof,'destination_identity':'b'*64},
                      {**proof,'started_at':proof['completed_at']+1},
                      {**proof,'run_count':True}):
            with self.subTest(value=type(value).__name__):
                d.atomic(first,json.dumps(value).encode())
                with self.assertRaises((ValueError,TypeError)):r.accepted(self.home,self.records[B][0],digest,self.signature)
                self.assertTrue((self.pool/A).exists())
        d.atomic(first,body)

    def test_tail_missing_lock_and_unwritable_audit_do_not_retry_successful_business(self):
        with self.locks() as fds:
            (self.state/'cost-refresh.lock').unlink()
            with patch.object(r,'once',side_effect=OSError('fixture audit denied')):
                result=r.after_publication(self.home,self.pool/C,self.config,time.time(),fds[0])
                self.assertEqual(result['status'],'failed');self.assertTrue(result['audit_unavailable'])
        self.assertTrue((self.pool/A).exists())

    def test_old_shared_entry_overrules_historical_same_revision_exclusive_index(self):
        # Original old full writer uses its shared immutable entry and no pool flag.
        old=self.pool.parent/'releases'/C
        d.stage_runtime(self.data,self.home,C)
        d.atomic(old/'Stats/publisher.py',b'# original legacy shared publisher fixture\n')
        self.current.unlink();self.current.symlink_to(old)
        args=d.publisher_arguments(self.home,old,install=True)
        old_job={'Label':d.LABEL,'StartInterval':1800,'ProgramArguments':args}
        d.atomic(self.job,plistlib.dumps(old_job))
        self.assertEqual(d.selected_publisher_runtime(self.home,C,old_job),old)
        self.assertTrue((self.pool/C).exists())
        updated,_=d.local_refresh_inputs(self.home,'e'*40,C,json.loads(self.config.read_bytes()),old_job)
        self.assertNotIn('publisher_policy',updated)
        self.assertEqual(self.current.resolve(),old)

    def test_native_cadence_unknown_or_changed_cannot_accept(self):
        arguments=r.expected_arguments(self.home,self.records[C][0])
        body='arguments = {\n'+'\n'.join(arguments)+'\n}\npid = 123\nruns = 2\n'
        from types import SimpleNamespace
        for interval in ('','run interval = 900 seconds\n'):
            with patch.object(r.subprocess,'run',return_value=SimpleNamespace(stdout=(body+interval).encode())):
                with self.assertRaises(ValueError):r.loaded(self.home)
        with patch.object(r.subprocess,'run',return_value=SimpleNamespace(stdout=(body+'run interval = 1800 seconds\n').encode())):
            self.assertEqual(r.loaded(self.home),(arguments,123,2))

    def test_same_bytes_reference_replacement_invalidates_snapshot(self):
        from types import SimpleNamespace
        def observe(argv,**kwargs):
            if argv[0]=='/usr/bin/crontab':return SimpleNamespace(returncode=1,stdout=b'',stderr=b'no crontab for fixture')
            return SimpleNamespace(returncode=1 if argv[0]=='/usr/sbin/lsof' else 0,stdout=b'',stderr=b'')
        with patch.object(r.subprocess,'run',side_effect=observe):
            before=r.reference_snapshot(self.home,self.pool/A)
            d.atomic(self.config,self.config.read_bytes())
            after=r.reference_snapshot(self.home,self.pool/A)
        self.assertEqual(before[str(self.config)]['sha256'],after[str(self.config)]['sha256'])
        self.assertNotEqual(before,after)


if __name__=='__main__':unittest.main()
