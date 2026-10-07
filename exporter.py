#!/usr/bin/env python3
"""Read-only local RPC exporter; never imports keys or opens application storage."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import base64
import json
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request


def rpc(config, node, method, **params):
    address = f"http://127.0.0.1:{config['base_port'] + node * 10 + 1}/{method}"
    if params:
        address += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(address, timeout=2) as response:
        value = json.load(response)
    if "error" in value:
        raise ValueError("RPC error")
    return value["result"]


def collect_node(config, node):
    labels = {"node": f"node{node}", "chain_id": config["chain_id"]}
    rows = [("cpc_node_up", labels, 0)]
    started = time.monotonic()
    try:
        status = rpc(config, node, "status")
        if status["node_info"]["network"] != config["chain_id"]:
            raise ValueError("wrong chain")
        sync = status["sync_info"]
        rows[0] = ("cpc_node_up", labels, 1)
        rows.extend([("cpc_block_height", labels, int(sync["latest_block_height"])),
            ("cpc_node_voting_power", labels, int(status.get("validator_info", {}).get("voting_power", 0))),
            ("cpc_catching_up", labels, int(sync["catching_up"]))])
        timestamp = datetime.fromisoformat(sync["latest_block_time"].replace("Z", "+00:00")).timestamp()
        rows.append(("cpc_last_block_timestamp_seconds", labels, timestamp))
        mempool = rpc(config, node, "num_unconfirmed_txs")
        rows.append(("cpc_mempool_transactions", labels, int(mempool["total"])))
        response = rpc(config, node, "abci_query", path=json.dumps("/state"))["response"]
        if int(response.get("code", 0)):
            raise ValueError("application not initialized")
        state = json.loads(base64.b64decode(response["value"]))
        if state["chain_id"] != config["chain_id"]:
            raise ValueError("wrong application chain")
        rows.extend([("cpc_application_height", labels, state["height"]),
            ("cpc_supply_cpc", labels, state["supply"] / 10**18),
            ("cpc_burned_cpc", labels, state["burned"] / 10**18),
            ("cpc_accounts", labels, len(state["accounts"]))])
        rows.append(("cpc_application_up", labels, 1))
        validators = state.get("validators", {})
        rows.extend([
            ("cpc_bonded_cpc", labels, sum(v["self_stake"]+sum(d["amount"] for d in v["delegations"].values()) for v in validators.values())/10**18),
            ("cpc_unbonding_cpc", labels, sum(q["amount"] for q in state.get("unbondings", []))/10**18),
            ("cpc_scheduled_validators", labels, len(state.get("engine_powers", {}))),
        ])
    except (OSError, ValueError, KeyError, TypeError):
        rows.append(("cpc_application_up", labels, 0))
    rows.append(("cpc_rpc_poll_seconds", labels, time.monotonic() - started))
    return rows


def collect_load(root):
    try:
        report = json.loads((root / "load-latest.json").read_text())
        labels = {"run_id": report["run_id"]}
        rows = [("cpc_load_running", {}, int(report["status"] in ("preparing", "running", "draining")
            and time.time() - report["updated_at"] < 20))]
        for key in ("submitted", "confirmed", "execution_failed", "checktx_rejected", "broadcast_errors"):
            rows.append((f"cpc_load_{key}_total", labels, report[key]))
        for key in ("target_tps", "unresolved", "observed_confirmed_tps"):
            rows.append(("cpc_load_" + key, labels, report[key]))
        for quantile, value in report.get("latency_seconds", {}).items():
            rows.append(("cpc_load_confirmation_latency_seconds", {**labels, "quantile": quantile}, value))
        return rows
    except (OSError, ValueError, KeyError, TypeError):
        return [("cpc_load_running", {}, 0)]


def render(rows):
    types = set()
    lines = []
    for name, labels, value in rows:
        if name not in types:
            types.add(name)
            lines.append(f"# TYPE {name} {'counter' if name.endswith('_total') else 'gauge'}")
        tags = ",".join(f"{key}={json.dumps(str(val))}" for key, val in sorted(labels.items()))
        lines.append(name + ("{" + tags + "}" if tags else "") + " " + str(value))
    return ("\n".join(lines) + "\n").encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    config = json.loads(args.network.read_text())
    cache = {"body": b"", "updated": 0}
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=5)

    def poll():
        while True:
            rows = []
            for result in pool.map(lambda i: collect_node(config, i), range(5)):
                rows.extend(result)
            rows.extend(collect_load(args.network.parent))
            with lock:
                cache.update(body=render(rows), updated=time.time())
            time.sleep(5)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                body = cache["body"]
                healthy = time.time() - cache["updated"] < 20
            if self.path not in ("/metrics", "/health"):
                self.send_error(404)
                return
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body if self.path == "/metrics" else b"ready\n")
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=poll, daemon=True).start()
    print(f"Read-only exporter: 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
