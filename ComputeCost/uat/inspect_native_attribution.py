"""Bounded read-only diagnosis of enrolled native provenance. No transcript export."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'ComputeCost'), str(ROOT/'ComputeCost/uat')]
from weekly_reconciliation import candidate_registry
from pulse_compute_cost.pi_context import PiAncestry, _INPUT, _text, tenant_directory
from pulse_compute_cost.source_layout import manifest_paths, native_binding_source, pi_files, source_detail
from pulse_compute_cost.scl_session_attribution import read_bindings, pi_host_ref
from pulse_compute_cost.receipts import reference, digest, canonical
from pulse_compute_cost.events import parse_timestamp


def inspect_input(graph, row):
    content = _text(row.get('message', {}).get('content'))
    hit = _INPUT.search(content) if content else None
    result = {'native_ref': reference('native_record', digest(row)), 'at': row.get('timestamp'),
              'text': content is not None, 'footer': bool(hit)}
    if not graph.source.get('human_input_directory'):
        return {**result, 'reason': 'input_directory_not_registered'}
    if not hit:
        return {**result, 'reason': 'native_host_footer_absent',
                'contains_host_marker': bool(content and 'Host human input reference:' in content)}
    path = Path(graph.source['human_input_directory'])/(hit[1]+'.json')
    if not path.is_file():
        return {**result, 'reason': 'referenced_input_missing'}
    record = json.loads(path.read_bytes()); origin = record.get('source', {})
    values = [origin.get('channel'), origin.get('eventId'), origin.get('humanRef'), record.get('poolKey')]
    pool = record.get('poolKey'); original = origin.get('message'); before = content[:hit.start()]
    result.update(channel=origin.get('channel'),
        ref_matches=hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()==hit[1],
        tenant_matches=isinstance(pool,str) and graph.path.parent.name==tenant_directory(pool),
        original_equal=isinstance(original,str) and before==original,
        original_suffix=isinstance(original,str) and before.endswith('\n\n'+original),
        original_contained=isinstance(original,str) and original in before,
        original_bytes=len(original.encode()) if isinstance(original,str) else None,
        native_bytes=len(before.encode()), owner_matches=path.stat().st_uid==os.getuid(),
        private_mode=(path.stat().st_mode & 0o077)==0,
        extra_record_keys=sorted(set(record)-{'source','poolKey','observedAtMs'}))
    ms=record.get('observedAtMs')
    result['time_matches'] = type(ms) is int and datetime.fromtimestamp(ms/1000,timezone.utc)<=parse_timestamp(row.get('timestamp'),'native timestamp')
    try:
        proof=graph._input(row)
        result['reason']='verified' if proof else next((name for name in ('tenant_matches','time_matches') if not result[name]), 'original_text_mismatch')
    except Exception as exc:
        result['reason']=type(exc).__name__
        result['error']=str(exc) if '/' not in str(exc) and len(str(exc))<160 else 'source_validation_failed'
    return result


def inspect(site):
    registry=candidate_registry(site)
    result={'site':site,'at':datetime.now(timezone.utc).isoformat(),'sources':[], 'production_modified':False}
    for source in registry['sources']:
        if source['kind']!='pi':continue
        detail=source_detail(source)
        roots=manifest_paths(source) or [Path(p) for p in source.get('paths',[source.get('path')])]
        files=sorted({p for root in roots for p in pi_files(root,source.get('layout'),detail)})
        report={'source_ref':reference('source',source['id']),'sessions':[], 'has_input_directory':bool(source.get('human_input_directory')), 'surface':source.get('scl_surface_id')}
        hosts=set()
        for path in files:
            graph=PiAncestry(path,source); counts=Counter(); inputs=[]; failed=[]; session=None
            input_ancestor={}; week_inputs={}; week_counts=Counter()
            today=datetime.now(timezone.utc)+timedelta(hours=8)
            week_start=(today-timedelta(days=today.weekday())).strftime('%Y-%m-%d')

            for line in path.open('rb'):
                if not line.endswith(b'\n'):break
                try:row=json.loads(line)
                except ValueError:counts['invalid_json']+=1;continue
                if row.get('type')=='session':
                    session=row.get('id');continue
                parent_present=row.get('parentId') in graph.contexts
                inherited=input_ancestor.get(row.get('parentId'))
                input_ancestor[row.get('id')]=inherited
                try:ctx=graph.consume(row)
                except Exception as exc:
                    counts['context_error_'+type(exc).__name__]+=1;continue
                msg=row.get('message',{})
                if isinstance(msg,dict) and msg.get('role') in {'user','human'}:
                    item=inspect_input(graph,row);inputs.append(item)
                    counts['input_'+item['reason']]+=1
                    text=_text(msg.get('content')) or ''
                    control=None
                    if text.startswith(('SCL_RESUME_PACKET/v1','SCL_LOCAL_RESUME_PACKET/v1')):
                        try:
                            packet=json.loads(text.splitlines()[-1])
                            control={k:packet.get(k) for k in ('schema_version','work_item_id','epoch','surface_id','operation_ref','session_ref')}
                        except ValueError:pass
                    item.update(control=control)
                    input_ancestor[row.get('id')]=item['native_ref']
                    week_inputs[item['native_ref']]=item
                native=msg.get('usage') if isinstance(msg,dict) else None
                native=native or row.get('usage')
                if isinstance(native,dict):
                    counts['usage_'+(ctx.channel or 'unknown')]+=1
                    if row.get('timestamp','') >= week_start:
                        week_counts[(ctx.channel or 'unknown',input_ancestor.get(row.get('id')))]+=1
                    if not ctx.channel:
                        counts['unknown_parent_present' if parent_present else 'unknown_parent_missing']+=1
                        if len(failed)<8:failed.append({'ref':reference('native_record',digest(row)), 'at':row.get('timestamp'), 'parent_present':parent_present,'type':row.get('type'),'role':msg.get('role') if isinstance(msg,dict) else None})
            if isinstance(session,str):hosts.add(pi_host_ref(session))
            report['sessions'].append({'scope':reference('native_session',['pi',session]),'counts':dict(counts),'inputs':inputs,'samples':failed,'week_inputs':[{'channel':ch,'usage_count':count,'input':week_inputs.get(key)} for (ch,key),count in week_counts.items()]})
        try:
            selected=native_binding_source(source)
            if selected:
                root=Path(selected['scl_kernel_root'])
                report['kernel']={'root_exists':root.is_dir(),'pointer_exists':(root/'active-lineage.json').is_file(),'surface':selected['scl_surface_id']}
                bound=read_bindings(selected,hosts)
                report['bindings']=[{'host_ref':b.host_ref,'wi':b.wi,'agent':b.agent,'start':b.start.isoformat(),'end':b.end.isoformat() if b.end else None,'evidence':b.evidence_ref} for b in bound]
        except Exception as exc:
            report['binding_error']={'type':type(exc).__name__,'reason':str(exc) if '/' not in str(exc) and len(str(exc))<160 else 'source_path_or_validation_error'}
        result['sources'].append(report)
    return result


def main():
    import argparse, subprocess
    import weekly_reconciliation as replay
    parser=argparse.ArgumentParser()
    parser.add_argument('--site');parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--revision');args=parser.parse_args()
    args.output.mkdir(mode=0o700,parents=True,exist_ok=True)
    if args.site:
        replay.private(args.output/'peer.json.gz',gzip.compress(canonical({'facts':[],'result':inspect(args.site)})))
        return
    archive=subprocess.run(['git','-C',str(ROOT),'archive',args.revision,'ComputeCost','Stats','config/scl-surfaces.yaml'],check=True,stdout=subprocess.PIPE).stdout
    old="command=[sys.executable,'-B',str(source/'ComputeCost/uat/weekly_reconciliation.py'),'--site',sys.argv[1],'--output',str(out),'--peer']"
    new="command=[sys.executable,'-B',str(source/'ComputeCost/uat/inspect_native_attribution.py'),'--site',sys.argv[1],'--output',str(out)]"
    assert old in replay.RECEIVER
    replay.RECEIVER=replay.RECEIVER.replace(old,new)
    config=json.loads((Path.home()/'.local/state/pulse/stats/refresh.json').read_bytes())
    results=[inspect('ownermac')]
    for peer in [p for p in config['peers'] if p['id'] not in replay.paused_peers(config)]:
        _,report=replay.peer_replay(peer,archive,args.output)
        results.append(report)
    for report in results:
        replay.private(args.output/(report['site']+'-context.json'),canonical(report))
        for source in report['sources']:
            total=Counter()
            for session in source['sessions']:total.update(session['counts'])
            print(json.dumps({'site':report['site'],'source':source['source_ref'],'counts':dict(total),'binding_error':source.get('binding_error'),'binding_count':len(source.get('bindings',[]))}),flush=True)


if __name__=='__main__':main()
