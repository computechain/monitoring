"""Chain-scoped fleet dashboard and alert rules; no multiplied replica totals."""
from copy import deepcopy
import json
from pathlib import Path

SOURCE=Path(__file__).resolve().parent/'grafana/dashboards/computechain.json'


def selector(expr,chain):
    # Reference dashboard has only these metric tokens, with optional existing selectors.
    import re
    return re.sub(r'\b(cpc_[a-z0-9_]+|cometbft_[a-z0-9_]+)(\{[^}]*\})?',
        lambda m:m[1]+'{chain_id='+json.dumps(chain)+((','+m[2][1:-1]) if m[2] and m[2]!='{}' else '')+'}',expr)


def dashboards(config):
    template=json.loads(SOURCE.read_text())
    fleet=deepcopy(template)
    fleet.update(uid='computechain-fleet',title='ComputeChain — multisite WAN fleet',refresh='15s')
    fleet['description']='Seven node observations; heights/health are per node. TPS and supply are not summed across replicas. Remote ABCI/native metrics remain private.'
    expressions={
        1:['sum(cpc_node_up)'],2:['max(cpc_block_height)'],
        3:['max(cpc_block_height)-min(cpc_block_height)'],4:['max(cpc_mempool_transactions)'],
        5:['cpc_block_height','cpc_application_height'],
        6:['rate(cometbft_consensus_total_txs{node="validator-a1"}[1m])'],
        10:['cpc_mempool_transactions'],
        11:['rate(cometbft_consensus_block_interval_seconds_count{node="validator-a1"}[1m])'],
        12:['cometbft_p2p_peers'],13:['cpc_supply_cpc{node="full-a1"}','cpc_burned_cpc{node="full-a1"}'],
        14:['time()-cpc_last_block_timestamp_seconds'],15:['cpc_node_up','cpc_application_up'],
        17:['cpc_node_voting_power'],
        18:['cpc_bonded_cpc{node="full-a1"}','cpc_unbonding_cpc{node="full-a1"}'],
        19:['cpc_scheduled_validators{node="full-a1"}']}
    titles={1:'Reachable nodes / expected 7',5:'Engine heights (all) / application heights (local only)',
            4:'Mempool (local only)',6:'Finalized TPS — one replica, validator-a1',10:'Local mempool',
            11:'Blocks per second — validator-a1',12:'Peer counts (local native metrics only)',
            13:'Chain supply / burned CPC — full-a1',15:'RPC health (all) / ABCI health (local only)',
            18:'Chain bonded / unbonding CPC — full-a1',19:'Scheduled validators — full-a1'}
    fleet['panels']=[p for p in fleet['panels'] if p['id'] in expressions]
    for index,panel in enumerate(fleet['panels']):
        panel['title']=titles.get(panel['id'],panel['title'])
        if index>=4:
            panel['gridPos']={'x':((index-4)%2)*12,'y':4+((index-4)//2)*8,'w':12,'h':8}
        for target,expr in zip(panel['targets'],expressions[panel['id']]):
            target['expr']=selector(expr,config['chain_id'])
            target['legendFormat']='{{node}} · {{location}}'
        if panel['type']=='stat':
            panel['options']['reduceOptions']['calcs']=['last']  # Missing is not last-good success.
    start=max(p['gridPos']['y']+p['gridPos']['h'] for p in fleet['panels'])
    extras=[('Common block/AppHash agreement (1=yes, 0=conflict; missing=unknown)', 'cpc_fleet_block_agreement'),
            ('Agreement witnesses available (all 7 required)', 'cpc_fleet_agreement_available'),
            ('Catching up / block lag by node', 'cpc_catching_up'),
            ('Location observed native power — NOT signing quorum', 'sum by (location) (cpc_node_voting_power * cpc_node_up)'),
            ('Native total power', 'cpc_fleet_total_voting_power'),
            ('Collection age (seconds)', 'time()-cpc_fleet_observation_timestamp_seconds'),
            ('Height behind highest observed node', 'scalar(max(cpc_block_height))-cpc_block_height'),
            ('Firing alerts (empty = none)', 'ALERTS{chain_id='+json.dumps(config['chain_id'])+',alertstate="firing"}')]
    for i,(title,expr) in enumerate(extras):
        fleet['panels'].append({'id':100+i,'title':title,'type':'timeseries',
            'datasource':{'type':'prometheus','uid':'ds_prom'},
            'gridPos':{'x':(i%2)*12,'y':start+(i//2)*8,'w':12,'h':8},
            'targets':[{'refId':'A','expr':selector(expr,config['chain_id']),'legendFormat':'{{node}} {{location}} {{alertname}}'}]})
    legacy=deepcopy(template)
    legacy['title']='ComputeChain — historical local devnet (not WAN fleet)'
    for panel in legacy['panels']:
        for target in panel.get('targets',[]):
            target['expr']=selector(target['expr'],'cpc-comet-staking-devnet-1')
    return {'fleet.json':fleet,'computechain.json':legacy}


def alert_rules(chain):
    s=lambda expression:selector(expression,chain)
    alerts=[
        ('ComputeChainNodeUnavailable',s('cpc_node_up == 0'),'45s','warning','Node read source unavailable or identity/genesis rejected'),
        ('ComputeChainFinalityStalled',s('time()-max(cpc_last_block_timestamp_seconds)>30'),'30s','critical','No recent observed finalized block'),
        ('ComputeChainNodeLagging',s('scalar(max(cpc_block_height))-cpc_block_height>5'),'60s','warning','Node is more than five blocks behind'),
        ('ComputeChainBlockDisagreement',s('cpc_fleet_block_agreement == 0'),'15s','critical','Observers disagree on a common block/AppHash'),
        ('ComputeChainAgreementUnavailable',s('cpc_fleet_agreement_available == 0'),'60s','warning','Cannot compare all fleet observers'),
        ('ComputeChainCollectorStale',s('time()-cpc_fleet_observation_timestamp_seconds>45')+' or absent('+s('cpc_fleet_observation_timestamp_seconds')+')','45s','critical','Fleet observer is missing or stale'),
    ]
    return {'groups':[{'name':'computechain-fleet','rules':[{'alert':name,'expr':expr,'for':duration,
        'labels':{'severity':severity,'chain_id':chain},'annotations':{'summary':description}}
        for name,expr,duration,severity,description in alerts]}]}
