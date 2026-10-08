#!/usr/bin/env python3
"""Control only this devnet's Compose project; preserve all monitoring volumes."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parent
sys.path.insert(0,str(REPO.parent))


def project(root):
    return "cpc-monitor-" + hashlib.sha256(str(root).encode()).hexdigest()[:10]


def resolve_host(value=None):
    """Bind monitoring to one LAN address, not every/public interface."""
    if value is None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("192.0.2.1", 9))  # Route lookup only; no packet is sent.
            value = route.getsockname()[0]
    address = ipaddress.ip_address(value)
    private_ranges = [ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    if address.version != 4 or not (address.is_loopback or any(address in net for net in private_ranges)):
        raise ValueError("monitoring host must be a literal private LAN IPv4 or loopback address")
    return str(address)


def configure(root, exporter_port, prometheus_port, grafana_port, monitoring_host=None, *, fleet_file=None, fleet_sha256=None):
    directory = root / "monitoring"
    directory.mkdir(mode=0o700, exist_ok=True)
    selection = directory / 'fleet-selection.json'
    fleet_config = None
    chosen = None
    if fleet_file is not None:
        chosen = {'file':str(Path(fleet_file).resolve()),'sha256':fleet_sha256,'exporter_port':exporter_port}
    elif selection.exists():
        chosen = json.loads(selection.read_text())
    if chosen:
        # An old stand launcher must not silently redirect the shared UI back to
        # its historical chain. Selection persists until explicit operator change.
        from monitoring.fleet import load
        path = Path(chosen['file'])
        if path.is_symlink() or not isinstance(chosen['sha256'],str) or hashlib.sha256(path.read_bytes()).hexdigest()!=chosen['sha256']:
            raise ValueError('approved fleet monitoring config SHA mismatch')
        fleet_config = load(path)
        exporter_port = chosen['exporter_port']
        if type(exporter_port) is not int or not 1024<=exporter_port<=65535:
            raise ValueError('invalid loopback fleet exporter port')
    envfile = directory / "monitoring.env"
    saved = dict(line.split("=", 1) for line in envfile.read_text().splitlines() if "=" in line) if envfile.exists() else {}
    host = resolve_host(monitoring_host or saved.get("MONITORING_HOST"))
    if saved and (int(saved["PROMETHEUS_PORT"]) != prometheus_port or int(saved["GRAFANA_PORT"]) != grafana_port):
        raise ValueError("monitoring ports already fixed for this directory; use saved ports or a new --dir")
    if not envfile.exists():
        descriptor = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(f"GRAFANA_ADMIN_PASSWORD={secrets.token_urlsafe(32)}\nPROMETHEUS_PORT={prometheus_port}\nGRAFANA_PORT={grafana_port}\n")
    saved = dict(line.split("=", 1) for line in envfile.read_text().splitlines() if "=" in line)
    if saved.get("MONITORING_HOST") != host:
        saved["MONITORING_HOST"] = host
        # Preserve the existing password/ports while upgrading old loopback config.
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as temporary:
            temporary.write("".join(f"{key}={value}\n" for key, value in saved.items()))
            name = temporary.name
        os.replace(name, envfile)  # Temp files are 0600, including during creation.
    config = json.loads((root / "network.json").read_text())
    if fleet_config:
        from monitoring.fleet_dashboards import dashboards, alert_rules
        targets = [{'targets':[node['metrics']], 'labels':{
            'node':node['name'],'chain_id':fleet_config['chain_id'],'location':node['location'],
            'machine':node['machine'],'role':node['role']}}
            for node in fleet_config['nodes'] if node['metrics'] is not None]
        dashboards_directory=directory/'dashboards'
        dashboards_directory.mkdir(exist_ok=True)
        for filename,dashboard in dashboards(fleet_config).items():
            (dashboards_directory/filename).write_text(json.dumps(dashboard,indent=2)+'\n')
        (directory/'fleet-alerts.json').write_text(json.dumps(alert_rules(fleet_config['chain_id']),indent=2)+'\n')
    else:
        targets = [{"targets": [f"127.0.0.1:{config['base_port'] + i * 10 + 3}"],
                    "labels": {"node": f"node{i}", "chain_id":config.get('chain_id','local-devnet')}} for i in range(5)]
    # JSON is valid YAML; Prometheus accepts it without adding a YAML dependency.
    rules = {"global": {"scrape_interval": "5s", "evaluation_interval": "5s"}, "scrape_configs": [
        {"job_name": "cometbft", "static_configs": targets},
        {"job_name": "cpc-application", "static_configs": [{"targets": [f"127.0.0.1:{exporter_port}"]}]},
        {"job_name": "prometheus", "static_configs": [{"targets": [f"{host}:{prometheus_port}"]}]}]}
    if fleet_config:
        rules['scrape_configs'][1]['job_name']='cpc-fleet'
        rules['scrape_configs'][1]['scrape_interval']='15s'
        rules['rule_files']=['/etc/prometheus/fleet-alerts.json']
    (directory / "prometheus.json").write_text(json.dumps(rules, indent=2) + "\n")
    datasource = {"apiVersion": 1, "datasources": [{"name": "Prometheus", "uid": "ds_prom", "type": "prometheus",
        "access": "proxy", "url": f"http://{host}:{prometheus_port}", "isDefault": True, "editable": False}]}
    provisioning = directory / "datasources"
    provisioning.mkdir(exist_ok=True)
    (provisioning / "prometheus.yaml").write_text(json.dumps(datasource, indent=2) + "\n")
    if chosen:
        selection.write_text(json.dumps(chosen,indent=2)+'\n')
    elif not (directory/'fleet-alerts.json').exists():
        (directory/'fleet-alerts.json').write_text('{"groups":[]}\n')
    return envfile


def compose(root, *args):
    directory = root / "monitoring"
    envfile = directory / "monitoring.env"
    env = {**os.environ, "MONITORING_RUNTIME": str(directory)}
    if (directory/'fleet-selection.json').exists():
        env['GRAFANA_DASHBOARDS']=str(directory/'dashboards')
        env['GRAFANA_HOME_DASHBOARD']='/var/lib/grafana/dashboards/fleet.json'
    return subprocess.run(["docker", "compose", "--project-name", project(root), "--env-file", str(envfile),
        "--file", str(REPO / "docker-compose.yml"), *args], env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "down", "status", "logs", "check"])
    parser.add_argument("--dir", type=Path, default=REPO.parent / ".runtime/comet-staking-devnet")
    parser.add_argument("--exporter-port", type=int)
    parser.add_argument("--prometheus-port", type=int, default=9090)
    parser.add_argument("--grafana-port", type=int, default=3000)
    parser.add_argument("--monitoring-host", help="private LAN IPv4; default: detected host address (saved per devnet)")
    parser.add_argument('--fleet',type=Path,help='explicit approved fleet observer config (UI data selection persists)')
    parser.add_argument('--fleet-sha256',help='approved public config SHA256; required with --fleet')
    args = parser.parse_args()
    if bool(args.fleet)!=bool(args.fleet_sha256):
        parser.error('--fleet and --fleet-sha256 must be provided together')
    root = args.dir.resolve()
    if not (root / "network.json").is_file():
        parser.error("initialize the Comet devnet first")
    if args.command in ("up", "check"):
        config = json.loads((root / "network.json").read_text())
        envfile = configure(root, args.exporter_port or config["base_port"] + 4, args.prometheus_port, args.grafana_port, args.monitoring_host,
                            fleet_file=args.fleet,fleet_sha256=args.fleet_sha256)
        if args.command == "up":
            compose(root, "up", "-d", "--wait", "--wait-timeout", "90")
            saved = dict(line.split("=", 1) for line in envfile.read_text().splitlines() if "=" in line)
            uid='computechain-fleet' if (root/'monitoring/fleet-selection.json').exists() else 'computechain-v2'
            print(f"Grafana: http://{saved['MONITORING_HOST']}:{args.grafana_port}/d/{uid}", flush=True)
            print(f"Prometheus: http://{saved['MONITORING_HOST']}:{args.prometheus_port}", flush=True)
            print(f"Credentials: admin; password stored only in {root}/monitoring/monitoring.env", flush=True)
        else:
            compose(root, "config", "--quiet")
    elif not (root / "monitoring/monitoring.env").exists():
        print("Monitoring not configured for this directory.")
    elif args.command == "down":
        compose(root, "down")  # No --volumes; no other project's containers touched.
    elif args.command == "logs":
        compose(root, "logs", "--tail", "60")
    else:
        compose(root, "ps")


if __name__ == "__main__":
    main()
