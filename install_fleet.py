#!/usr/bin/env python3
"""Install only a new local read-only fleet observer; no validator/SSH changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from monitoring import fleet
from computechain.scripts import multisite
from computechain.scripts.install_multisite import safe_root


def install(root,config_file,approved_sha,port):
    if os.geteuid()!=0:
        raise ValueError('explicit root operator required')
    root=safe_root(root)
    raw=Path(config_file).read_bytes()
    if len(raw)>fleet.MAX_CONFIG or hashlib.sha256(raw).hexdigest()!=approved_sha:
        raise ValueError('approved fleet config SHA mismatch')
    config=fleet.load(config_file)
    if type(port) is not int or not 1024<=port<=65535:
        raise ValueError('invalid observer port')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',port))
    for node in config['nodes']:
        if node['rpc'].startswith('http://'):
            m=multisite.checked_home(root/config['chain_id']/'nodes'/node['name'])
            if m['registration']['node_id']!=node['node_id'] or m['genesis_sha256']!=config['genesis_sha256'] or node['rpc']!=f"http://127.0.0.1:{m['ports']['rpc']}" or node['metrics']!=f"127.0.0.1:{m['ports']['metrics']}":
                raise ValueError('local observer endpoints differ from installed fleet')
    home=root/'fleet-monitoring'; runtime=root/'monitoring-runtime'
    unit=Path('/etc/systemd/system/cpc-fleet-observer.service')
    if any(p.exists() or p.is_symlink() for p in (home,runtime,unit)):
        raise ValueError('observer installation must be NEW; explicit review required for upgrades')
    username='cpm-fleet'
    try:
        account=pwd.getpwnam(username)
        if account.pw_dir!=str(home) or account.pw_shell!='/usr/sbin/nologin':
            raise ValueError('existing account is not this observer')
    except KeyError:
        subprocess.run(['useradd','--system','--no-create-home','--home-dir',str(home),'--shell','/usr/sbin/nologin',username],check=True)
    # Root-owned read-only PUBLIC trust files. This user gets no keys/node homes,
    # SSH credentials or writable operator configuration. The collector allowlist
    # performs no write RPC calls; loopback networking is not process isolation.
    home.mkdir(mode=0o755); runtime.mkdir(mode=0o755)
    for source,name in ((config['ca_file'],'ca.pem'),(config['genesis_file'],'genesis.json')):
        shutil.copyfile(source,home/name); (home/name).chmod(0o644)
    config['ca_file']=str(home/'ca.pem'); config['genesis_file']=str(home/'genesis.json')
    fleet.configuration(config)
    path=home/'fleet.json'; path.write_text(json.dumps(config,indent=2)+'\n'); path.chmod(0o644)
    source_root=Path(__file__).resolve().parent
    for name in ('fleet.py','exporter.py'):
        shutil.copyfile(source_root/name,runtime/name); (runtime/name).chmod(0o644)
    app_runtime=root/'runtime'
    unit.write_text(f'''[Unit]
Description=ComputeChain read-only WAN fleet observer
After=network-online.target
Wants=network-online.target
[Service]
User={username}
WorkingDirectory={runtime}
Environment=PYTHONPATH={runtime}:{app_runtime}:{app_runtime}/.deps
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 {runtime}/fleet.py --config {path} --port {port}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=tmpfs
BindReadOnlyPaths={runtime} {app_runtime} {home}
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
ProtectKernelModules=true
RestrictAddressFamilies=AF_INET AF_UNIX
CapabilityBoundingSet=
LockPersonality=true
LimitCORE=0
MemoryMax=96M
CPUQuota=10%
TasksMax=16
UMask=0077
[Install]
WantedBy=multi-user.target
''')
    unit.chmod(0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now',unit.name],check=True)
    result={'chain_id':config['chain_id'],'config':str(path),'config_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'exporter_port':port,'unit':unit.name,'private_keys_mounted':False,'validators_restarted':False}
    (home/'installation.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--config-sha256',required=True)
    parser.add_argument('--port',required=True,type=int)
    args=parser.parse_args()
    print(json.dumps(install(args.root,args.config,args.config_sha256,args.port),indent=2))
