"""Fleet identities, read boundary, offline behavior and chain-scoped dashboards."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import time

import pytest
from monitoring import fleet, stack, fleet_dashboards
from computechain.scripts import bootstrap_pki as pki, signed_checkpoint as signed


@pytest.fixture
def config(tmp_path):
    ca=pki.ca_init(tmp_path/'ca')
    genesis={'chain_id':'fleet-test','genesis_time':'2026-10-08T05:03:41.982680Z','app_state':{'schema':3}}
    genesis_file=tmp_path/'genesis.json'; genesis_file.write_text(json.dumps(genesis))
    digest=hashlib.sha256(genesis_file.read_bytes()).hexdigest()
    public=signed.key_init(tmp_path/'authority.hex')['public_key']
    nodes=[]; providers=[]
    names=('validator-a1','validator-a2','validator-b1','validator-c1','full-a1','full-a2','full-a3')
    for i,name in enumerate(names):
        remote=i in (2,3)
        url=('https://178.72.89.199:27626' if i==2 else 'https://93.171.44.155:27636') if remote else f'http://127.0.0.1:{27601+10*i}'
        location='site-b' if i==2 else 'site-c' if i==3 else 'site-a'
        node={'name':name,'node_id':f'{i+1:040x}','location':location,'machine':'host-'+location,
              'role':'validator' if i<4 else 'full','rpc':url,'metrics':None if remote else f'127.0.0.1:{27603+10*i}'}
        nodes.append(node)
        if remote: providers.append({'name':name,'node_id':node['node_id'],'location':location,'url':url,'certificate_sha256':str(i)*64})
    return {'format':1,'chain_id':'fleet-test','genesis_sha256':digest,'genesis_file':str(genesis_file),
        'ca_file':str(ca),'nodes':nodes,'bootstrap_profile':{'format':1,'chain_id':'fleet-test','genesis_sha256':digest,
        'ca_sha256':hashlib.sha256(ca.read_bytes()).hexdigest(),'operator_public_key':public,'providers':providers}}


@pytest.fixture
def observer(config,monkeypatch):
    obj=fleet.Observer(config)
    state={'schema':3,'chain_id':config['chain_id'],'height':100,'supply':10**24,'burned':0,'accounts':{},
           'validators':{},'unbondings':[],'engine_powers':{}}
    def call(node,method,**params):
        if method=='status':
            return {'node_info':{'id':node['node_id'],'network':config['chain_id']},
                'validator_info':{'voting_power':'10000' if node['role']=='validator' else '0'},
                'sync_info':{'latest_block_height':'100','latest_block_time':'2026-10-08T00:00:00Z','catching_up':False}}
        if method=='genesis':
            g=json.loads(Path(config['genesis_file']).read_text()); g['genesis_time']='2026-10-08T05:03:41.98268Z'
            return {'genesis':g}
        if method=='block': return {'block_id':{'hash':'A'*64},'block':{'header':{'chain_id':config['chain_id'],'height':str(params['height']),'app_hash':'B'*64}}}
        if method=='validators': return {'total':'4','validators':[{'voting_power':'10000'}]*4}
        if method=='abci_query': return {'response':{'code':0,'value':base64.b64encode(json.dumps(state).encode()).decode()}}
        if method=='num_unconfirmed_txs': return {'total':'0'}
        raise AssertionError(method)
    monkeypatch.setattr(obj,'call',call)
    return obj


def values(rows): return {n:v for n,_,v in rows}


@pytest.mark.parametrize('fault',['public-rpc','hostname','credentials','rpc-path','remote-id','remote-location','remote-metrics','duplicate-name','duplicate-id','duplicate-rpc','missing-provider','unapproved-ca','changed-genesis'])
def test_unapproved_sources_fail_before_poll(config,fault):
    c=deepcopy(config); n=c['nodes'][0]
    if fault=='public-rpc': n['rpc']='http://78.29.35.87:27601'
    if fault=='hostname': n['rpc']='http://localhost:27601'
    if fault=='credentials': n['rpc']='http://admin:secret@127.0.0.1:27601'
    if fault=='rpc-path': n['rpc']+='/status'
    if fault=='remote-id': c['nodes'][2]['node_id']='f'*40
    if fault=='remote-location': c['nodes'][2]['location']='site-a'
    if fault=='remote-metrics': c['nodes'][2]['metrics']='178.72.89.199:27623'
    if fault=='duplicate-name': c['nodes'][1]['name']=n['name']
    if fault=='duplicate-id': c['nodes'][1]['node_id']=n['node_id']
    if fault=='duplicate-rpc': c['nodes'][1]['rpc']=n['rpc']
    if fault=='missing-provider': c['nodes'].pop(2)
    if fault=='unapproved-ca': c['bootstrap_profile']['ca_sha256']='f'*64
    if fault=='changed-genesis': Path(c['genesis_file']).write_text('{}')
    with pytest.raises(ValueError): fleet.configuration(c)


def test_all_seven_nodes_and_common_commitments(observer):
    with ThreadPoolExecutor(max_workers=4) as pool: rows=observer.collect(pool)
    assert sum(v for n,_,v in rows if n=='cpc_node_up')==7
    assert sum(v for n,_,v in rows if n=='cpc_node_voting_power')==40000
    assert sum(1 for n,_,_ in rows if n=='cpc_application_up')==5
    assert values(rows)['cpc_fleet_block_agreement']==1
    assert values(rows)['cpc_fleet_total_voting_power']==40000
    assert all('chain_id' in labels for _,labels,_ in rows)


@pytest.mark.parametrize('fault',['offline','wrong-id','wrong-chain','wrong-genesis','invalid-power','negative-power','invalid-time'])
def test_bad_source_has_no_fake_height_or_green_agreement(observer,monkeypatch,fault):
    old=observer.call; failed=observer.config['nodes'][2]
    def call(node,method,**params):
        if node==failed:
            if fault=='offline': raise OSError('offline')
            reply=old(node,method,**params)
            if method=='status' and fault=='wrong-id': reply['node_info']['id']='f'*40
            if method=='status' and fault=='wrong-chain': reply['node_info']['network']='other'
            if method=='status' and fault=='invalid-power': reply['validator_info']['voting_power']='bad'
            if method=='status' and fault=='negative-power': reply['validator_info']['voting_power']='-1'
            if method=='status' and fault=='invalid-time': reply['sync_info']['latest_block_time']='no-time'
            if method=='genesis' and fault=='wrong-genesis': reply['genesis']['app_state']['schema']=2
            return reply
        return old(node,method,**params)
    monkeypatch.setattr(observer,'call',call)
    with ThreadPoolExecutor(max_workers=4) as pool: rows=observer.collect(pool)
    remote=[row for row in rows if row[1].get('node')==failed['name']]
    assert values(remote)['cpc_node_up']==0 and 'cpc_block_height' not in values(remote)
    assert values(rows)['cpc_fleet_agreement_available']==0
    assert 'cpc_fleet_block_agreement' not in values(rows)


def test_common_apphash_conflict_is_not_hidden(observer,monkeypatch):
    old=observer.call
    def call(node,method,**params):
        reply=old(node,method,**params)
        if method=='block' and node['name']=='validator-c1': reply['block']['header']['app_hash']='C'*64
        return reply
    monkeypatch.setattr(observer,'call',call)
    with ThreadPoolExecutor(max_workers=4) as pool: rows=observer.collect(pool)
    assert values(rows)['cpc_fleet_agreement_available']==1
    assert values(rows)['cpc_fleet_block_agreement']==0


def test_malformed_common_commitments_never_produce_green_agreement(observer,monkeypatch):
    old=observer.call
    def call(node,method,**params):
        reply=old(node,method,**params)
        if method=='block': reply['block_id']['hash']=''
        return reply
    monkeypatch.setattr(observer,'call',call)
    with ThreadPoolExecutor(max_workers=4) as pool: rows=observer.collect(pool)
    assert values(rows)['cpc_fleet_agreement_available']==0
    assert 'cpc_fleet_block_agreement' not in values(rows)


def test_remote_reads_never_open_abci_or_write_api(config,monkeypatch):
    observer=fleet.Observer(config); remote=config['nodes'][2]; calls=[]
    monkeypatch.setattr(fleet.bootstrap,'read',lambda p,c,m,**kw:calls.append(m) or {})
    for method in ('status','block','genesis','validators'): observer.call(remote,method)
    for method in ('abci_query','num_unconfirmed_txs','broadcast_tx_commit','net_info'):
        with pytest.raises(ValueError): observer.call(remote,method)
    assert calls==['status','block','genesis','validators']


def test_fleet_selection_keeps_password_volumes_and_survives_old_launcher(config,tmp_path):
    (tmp_path/'network.json').write_text(json.dumps({'chain_id':'old-chain','base_port':28600}))
    env=stack.configure(tmp_path,28604,9090,3000,'192.168.0.100'); before=env.read_bytes()
    profile=tmp_path/'fleet.json'; profile.write_text(json.dumps(config))
    sha=hashlib.sha256(profile.read_bytes()).hexdigest()
    stack.configure(tmp_path,27674,9090,3000,fleet_file=profile,fleet_sha256=sha)
    assert env.read_bytes()==before
    rules=json.loads((tmp_path/'monitoring/prometheus.json').read_text())
    assert len(rules['scrape_configs'][0]['static_configs'])==5
    assert all('276' in t['targets'][0] for t in rules['scrape_configs'][0]['static_configs'])
    assert rules['scrape_configs'][1]['static_configs'][0]['targets']==['127.0.0.1:27674']
    stack.configure(tmp_path,28604,9090,3000)  # Old launcher cannot revert fleet selection.
    assert json.loads((tmp_path/'monitoring/prometheus.json').read_text())==rules
    assert (tmp_path/'monitoring/dashboards/fleet.json').is_file()
    profile.write_text('{}')
    with pytest.raises(ValueError,match='SHA mismatch'): stack.configure(tmp_path,28604,9090,3000)
    assert json.loads((tmp_path/'monitoring/prometheus.json').read_text())==rules


def test_fleet_dashboards_scope_metrics_and_count_chain_once(config):
    dash=fleet_dashboards.dashboards(config)['fleet.json']; panels={p['id']:p for p in dash['panels']}
    expressions=[t['expr'] for p in dash['panels'] for t in p['targets']]
    assert all('chain_id="fleet-test"' in expr for expr in expressions)
    assert 'validator-a1' in panels[6]['targets'][0]['expr']
    assert 'sum' not in panels[6]['targets'][0]['expr']
    assert 'full-a1' in panels[13]['targets'][0]['expr']
    assert not {7,8,9,16}&set(panels)  # No unrelated legacy load metrics.
    alerts=fleet_dashboards.alert_rules(config['chain_id'])['groups'][0]['rules']
    assert len(alerts)==6 and all('chain_id="fleet-test"' in rule['expr'] for rule in alerts)
