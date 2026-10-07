"""Read-only exporter and runtime provisioning contracts (no Docker required)."""
import json
import base64
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from monitoring import exporter, stack


def test_runtime_targets_follow_custom_devnet_and_password_persists(tmp_path):
    config = {"base_port": 29600, "chain_id": "unit-monitoring"}
    (tmp_path / "network.json").write_text(json.dumps(config))
    envfile = stack.configure(tmp_path, 29604, 19090, 13000, "127.0.0.1")
    first = envfile.read_text()
    assert envfile.stat().st_mode & 0o777 == 0o600
    assert stack.configure(tmp_path, 29604, 19090, 13000).read_text() == first
    config = json.loads((tmp_path / "monitoring/prometheus.json").read_text())
    assert len(config["scrape_configs"][0]["static_configs"]) == 5
    assert config["scrape_configs"][0]["static_configs"][4]["targets"] == ["127.0.0.1:29643"]
    datasource = json.loads((tmp_path / "monitoring/datasources/prometheus.yaml").read_text())
    assert datasource["datasources"][0]["url"] == "http://127.0.0.1:19090"
    with pytest.raises(ValueError, match="ports already fixed"):
        stack.configure(tmp_path, 29604, 9090, 3000)


def test_monitoring_projects_are_isolated_per_network(tmp_path):
    assert stack.project(tmp_path / "a") != stack.project(tmp_path / "b")


def test_lan_upgrade_keeps_password_and_private_chain_targets(tmp_path):
    (tmp_path / "network.json").write_text(json.dumps({"base_port": 28600}))
    env = stack.configure(tmp_path, 28604, 9090, 3000, "127.0.0.1")
    initial = dict(line.split("=", 1) for line in env.read_text().splitlines())
    stack.configure(tmp_path, 28604, 9090, 3000, "192.168.1.10")
    saved = dict(line.split("=", 1) for line in env.read_text().splitlines())
    assert saved["GRAFANA_ADMIN_PASSWORD"] == initial["GRAFANA_ADMIN_PASSWORD"]
    assert saved["MONITORING_HOST"] == "192.168.1.10"
    config = json.loads((tmp_path / "monitoring/prometheus.json").read_text())
    assert config["scrape_configs"][0]["static_configs"][0]["targets"] == ["127.0.0.1:28603"]
    assert config["scrape_configs"][2]["static_configs"][0]["targets"] == ["192.168.1.10:9090"]


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "192.0.2.1", "localhost", "::"])
def test_no_wildcard_or_public_monitoring_bind(host):
    with pytest.raises(ValueError):
        stack.resolve_host(host)


def test_offline_node_has_no_fake_height(monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("offline")
    monkeypatch.setattr(exporter, "rpc", offline)
    rows = exporter.collect_node({"chain_id": "test", "base_port": 28600}, 0)
    assert next(value for name, _, value in rows if name == "cpc_node_up") == 0
    assert not any(name == "cpc_block_height" for name, _, _ in rows)


def test_voting_power_is_dynamic_and_stake_metrics_are_read_only(monkeypatch):
    state = {"chain_id": "test", "height": 10, "supply": 1000*10**18, "burned": 0,
        "accounts": {}, "validators": {"public": {"self_stake": 100*10**18, "delegations": {}}},
        "unbondings": [{"amount": 10**18}], "engine_powers": {"public": 100}}
    def response(config, node, method, **params):
        if method == "status":
            return {"node_info": {"network": "test"}, "validator_info": {"voting_power": "100"},
                "sync_info": {"latest_block_height": "10", "catching_up": False, "latest_block_time": "2026-10-07T00:00:00Z"}}
        if method == "num_unconfirmed_txs":
            return {"total": "0"}
        assert method == "abci_query"
        return {"response": {"value": base64.b64encode(json.dumps(state).encode()).decode()}}
    monkeypatch.setattr(exporter, "rpc", response)
    rows = exporter.collect_node({"chain_id": "test", "base_port": 28600}, 4)
    values = {name: value for name, _, value in rows}
    assert values["cpc_node_voting_power"] == 100  # node4 no longer hardcoded 'full'
    assert values["cpc_bonded_cpc"] == 100 and values["cpc_unbonding_cpc"] == 1
    assert all("role" not in labels for _, labels, _ in rows)


def test_load_counter_labels_and_stale_running_indicator(tmp_path):
    report = {"run_id": "unit-run", "status": "running", "updated_at": 0, "target_tps": 3, "unresolved": 2,
        "observed_confirmed_tps": 1, "submitted": 10, "confirmed": 8, "execution_failed": 0,
        "checktx_rejected": 0, "broadcast_errors": 0}
    (tmp_path / "load-latest.json").write_text(json.dumps(report))
    rows = exporter.collect_load(tmp_path)
    body = exporter.render(rows).decode()
    assert "cpc_load_running 0" in body
    assert 'cpc_load_confirmed_total{run_id="unit-run"} 8' in body


def test_dashboard_compares_series_without_or_dropping_same_labels():
    dashboard = json.loads((Path(__file__).resolve().parents[1] / "grafana/dashboards/computechain.json").read_text())
    load_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 7)
    assert len(load_panel["targets"]) == 2
    assert "submitted" in load_panel["targets"][0]["expr"]
    assert "confirmed" in load_panel["targets"][1]["expr"]
    assert all(target.get("datasource", {}).get("uid", "ds_prom") == "ds_prom"
        for panel in dashboard["panels"] for target in panel.get("targets", []))
