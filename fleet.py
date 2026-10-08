#!/usr/bin/env python3
"""Bounded read-only fleet observer: local RPC, pinned remote TLS, no SSH/keys."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request

from computechain.scripts import bootstrap_protocol as bootstrap
from computechain.scripts import comet_checkpoint as anchors
from computechain.scripts import rpc_read_gateway as reads
try:
    from monitoring.exporter import render
except ImportError:  # Compact deployment of these public sources.
    from exporter import render

MAX_CONFIG = 65536
POLL_SECONDS = 15


def configuration(value):
    fields = {'format', 'chain_id', 'genesis_sha256', 'genesis_file', 'bootstrap_profile', 'ca_file', 'nodes'}
    if not isinstance(value, dict) or set(value) != fields or type(value['format']) is not int or value['format'] != 1:
        raise ValueError('invalid fleet monitoring configuration')
    bootstrap.profile(value['bootstrap_profile'], value)
    ca = Path(value['ca_file'])
    if not ca.is_absolute():
        raise ValueError('CA must be an absolute approved public path')
    bootstrap.trust_context(ca, value['bootstrap_profile']['ca_sha256'])
    genesis = Path(value['genesis_file'])
    if not genesis.is_absolute() or genesis.is_symlink() or genesis.stat().st_size>65536:
        raise ValueError('bounded public genesis path required')
    anchors.local_genesis(value)  # Original file SHA remains pinned, not RPC serialization.
    nodes = value['nodes']
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= 16:
        raise ValueError('bounded fleet node list required')
    names, ids, endpoints, metric_ports = set(), set(), set(), set()
    providers = {p['name']: p for p in value['bootstrap_profile']['providers']}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {'name', 'node_id', 'location', 'machine', 'role', 'rpc', 'metrics'}:
            raise ValueError('invalid fleet node fields')
        for key in ('name', 'location', 'machine'):
            if not isinstance(node[key], str) or not re.fullmatch(r'[a-z][a-z0-9-]{1,39}', node[key]):
                raise ValueError('invalid fleet label')
        if node['role'] not in ('validator', 'full') or not isinstance(node['node_id'],str) or not re.fullmatch(r'[0-9a-f]{40}', node['node_id']):
            raise ValueError('invalid role/node identity')
        if not isinstance(node['rpc'],str):
            raise ValueError('invalid fleet RPC URL')
        if node['name'] in names or node['node_id'] in ids or node['rpc'] in endpoints:
            raise ValueError('duplicate fleet identity/endpoint')
        names.add(node['name']); ids.add(node['node_id']); endpoints.add(node['rpc'])
        if node['rpc'].startswith('https://'):
            p = providers.get(node['name'])
            if not p or any(p[k] != node[k] for k in ('node_id', 'location')) or p['url'] != node['rpc'] or node['metrics'] is not None:
                raise ValueError('remote node must match approved TLS provider; no public native metrics')
        else:
            origin = urllib.parse.urlsplit(node['rpc'])
            if origin.scheme != 'http' or origin.hostname != '127.0.0.1' or origin.netloc != f'127.0.0.1:{origin.port}' or origin.path or origin.query or origin.fragment or not 1024 <= origin.port <= 65535:
                raise ValueError('native RPC must be literal loopback only')
            if not isinstance(node['metrics'], str) or not re.fullmatch(r'127\.0\.0\.1:\d{4,5}', node['metrics']):
                raise ValueError('native metrics must be loopback only')
            port = int(node['metrics'].split(':')[1])
            if not 1024 <= port <= 65535 or port in metric_ports:
                raise ValueError('duplicate/invalid metrics port')
            metric_ports.add(port)
    if set(providers) - names:
        raise ValueError('approved remote providers missing from fleet')
    return value


def load(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('fleet config must not be a symlink')
    with path.open('rb') as stream:
        raw = stream.read(MAX_CONFIG + 1)
    if len(raw) > MAX_CONFIG:
        raise ValueError('oversized fleet config')
    return configuration(json.loads(raw, object_pairs_hook=reads.pairs))


class Observer:
    def __init__(self, config):
        self.config = configuration(config)
        self.tls = bootstrap.trust_context(config['ca_file'], config['bootstrap_profile']['ca_sha256'])
        self.providers = {p['name']: p for p in config['bootstrap_profile']['providers']}
        self.genesis = anchors._genesis_identity(anchors.local_genesis(config))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, node, method, **params):
        if method not in ('status', 'block', 'validators', 'genesis', 'abci_query', 'num_unconfirmed_txs'):
            raise ValueError('monitoring RPC allowlist')
        if node['rpc'].startswith('https://'):
            if method not in ('status', 'block', 'validators', 'genesis'):
                raise ValueError('remote monitoring never enables ABCI/mempool/write APIs')
            return bootstrap.read(self.providers[node['name']], self.tls, method, **params)
        url = node['rpc'] + '/' + method + '?' + urllib.parse.urlencode(params)
        with self.opener.open(url, timeout=2) as response:
            raw = response.read(reads.MAX_REPLY + 1)
        if len(raw) > reads.MAX_REPLY:
            raise ValueError('oversized monitoring RPC reply')
        value = json.loads(raw, object_pairs_hook=reads.pairs)
        if 'error' in value or not isinstance(value.get('result'), dict):
            raise ValueError('invalid monitoring RPC result')
        return value['result']

    def labels(self, node):
        return {'node': node['name'], 'chain_id': self.config['chain_id'],
                'location': node['location'], 'machine': node['machine'], 'role': node['role']}

    def collect_node(self, node):
        labels = self.labels(node)
        rows = [('cpc_node_up', labels, 0)]
        started = time.monotonic()
        status = None
        try:
            status = self.call(node, 'status')
            info, sync = status['node_info'], status['sync_info']
            if info['id'] != node['node_id'] or info['network'] != self.config['chain_id']:
                raise ValueError('wrong node/chain identity')
            if anchors._genesis_identity(self.call(node,'genesis')['genesis']) != self.genesis:
                raise ValueError('wrong genesis identity')
            height = int(sync['latest_block_height'])
            if height < 1 or type(sync['catching_up']) is not bool:
                raise ValueError('uninitialized or malformed native status')
            stamp = anchors.block_time_ns(sync['latest_block_time'])/10**9
            power = int(status['validator_info']['voting_power'])
            if power < 0:
                raise ValueError('invalid voting power')
            rows[0] = ('cpc_node_up', labels, 1)
            rows.extend([('cpc_block_height', labels, height),
                         ('cpc_node_voting_power', labels, power),
                         ('cpc_catching_up', labels, int(sync['catching_up'])),
                         ('cpc_last_block_timestamp_seconds', labels, stamp)])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            status = None
            rows = [('cpc_node_up', labels, 0)]
        if status and node['rpc'].startswith('http://'):
            try:
                response = self.call(node, 'abci_query', path='"/state"')['response']
                if int(response.get('code', 0)):
                    raise ValueError('local app unavailable')
                state = json.loads(base64.b64decode(response['value'], validate=True))
                if state['chain_id'] != self.config['chain_id'] or state['schema'] != 3:
                    raise ValueError('wrong local app domain')
                validators = state['validators']
                rows.extend([('cpc_application_up', labels, 1), ('cpc_application_height', labels, state['height']),
                    ('cpc_supply_cpc', labels, state['supply']/10**18), ('cpc_burned_cpc', labels, state['burned']/10**18),
                    ('cpc_accounts', labels, len(state['accounts'])),
                    ('cpc_bonded_cpc', labels, sum(v['self_stake']+sum(d['amount'] for d in v['delegations'].values()) for v in validators.values())/10**18),
                    ('cpc_unbonding_cpc', labels, sum(q['amount'] for q in state['unbondings'])/10**18),
                    ('cpc_scheduled_validators', labels, len(state['engine_powers']))])
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                rows.append(('cpc_application_up', labels, 0))
            try:
                rows.append(('cpc_mempool_transactions', labels, int(self.call(node, 'num_unconfirmed_txs')['total'])))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                pass
        rows.append(('cpc_rpc_poll_seconds', labels, time.monotonic()-started))
        return rows, status

    def collect(self, pool):
        nodes = self.config['nodes']
        observations = list(pool.map(self.collect_node, nodes))
        rows = [row for collected, _ in observations for row in collected]
        labels = {'chain_id': self.config['chain_id']}
        rows.append(('cpc_fleet_expected_nodes', labels, len(nodes)))
        rows.append(('cpc_fleet_observation_timestamp_seconds', labels, time.time()))
        good = [(n, s) for n, (_, s) in zip(nodes, observations) if s is not None]
        # Agreement needs all configured observers, not just the healthy majority.
        # Unknown/missing witnesses never produce a green agreement metric.
        agreement_available = False
        if len(good) == len(nodes):
            h = min(int(s['sync_info']['latest_block_height']) for _, s in good)-1
            try:
                blocks = list(pool.map(lambda n: self.call(n, 'block', height=h), nodes))
                identities = set()
                for block in blocks:
                    header = block['block']['header']
                    if header['chain_id'] != self.config['chain_id'] or int(header['height']) != h:
                        raise ValueError('common block domain/height mismatch')
                    if not re.fullmatch(r'[0-9A-F]{64}', block['block_id']['hash']) or not re.fullmatch(r'[0-9A-F]{64}',header['app_hash']):
                        raise ValueError('malformed common block commitments')
                    identities.add((block['block_id']['hash'], header['app_hash']))
                rows.extend([('cpc_fleet_block_agreement', labels, int(len(identities)==1)),
                             ('cpc_fleet_common_height', labels, h)])
                agreement_available = True
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                pass
        rows.append(('cpc_fleet_agreement_available', labels, int(agreement_available)))
        # Dynamic powers from the actual native set, not hardcoded genesis arithmetic.
        # Healthy status is only observed reachability, NOT proof of signing quorum.
        local = next((n for n, _ in good if n['rpc'].startswith('http://')), None)
        if local:
            try:
                reply = self.call(local, 'validators', per_page=100)
                validators = reply['validators']
                if int(reply['total']) != len(validators) or not 1 <= len(validators) <= 64:
                    raise ValueError('incomplete or oversized native validator set')
                powers = [int(v['voting_power']) for v in validators]
                if any(power<=0 for power in powers):
                    raise ValueError('invalid native power set')
                rows.append(('cpc_fleet_total_voting_power', labels, sum(powers)))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                pass
        return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('nonprivileged loopback port required')
    observer = Observer(load(args.config))
    pool = ThreadPoolExecutor(max_workers=4)
    if args.once:
        try: print(render(observer.collect(pool)).decode(), end='')
        finally: pool.shutdown(wait=True)
        return
    cache, lock = {'body': b'', 'updated': 0}, threading.Lock()

    def poll():
        while True:
            try:
                body = render(observer.collect(pool))
                with lock: cache.update(body=body, updated=time.monotonic())
            except Exception:
                pass  # Stale cache becomes HTTP503; no false last-good success.
            time.sleep(POLL_SECONDS)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()
        def do_GET(self):
            if self.path not in ('/metrics', '/health'):
                self.send_error(404); return
            with lock:
                body = cache['body']; healthy = bool(body) and time.monotonic()-cache['updated']<45
            self.send_response(200 if healthy else 503)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.end_headers()
            self.wfile.write(body if healthy and self.path=='/metrics' else b'ready\n' if healthy else b'stale\n')
        def log_message(self, *_): pass

    server = HTTPServer(('127.0.0.1', args.port), Handler)
    threading.Thread(target=poll, daemon=True).start()
    server.serve_forever()


if __name__ == '__main__': main()
