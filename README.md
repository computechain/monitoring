# ComputeChain Monitoring Stack

Prometheus + Grafana stack for monitoring a 24-hour test run.

## Startup

```bash
cd monitoring
docker-compose up -d
```

## Access

* **Grafana**: [http://localhost:3000](http://localhost:3000)

  * Username: `admin`
  * Password: `admin`
  * Dashboard: **ComputeChain** is automatically loaded

* **Prometheus**: [http://localhost:9090](http://localhost:9090)

  * Targets: [http://localhost:9090/targets](http://localhost:9090/targets)

## Shutdown

```bash
cd monitoring
docker-compose down
```

## Data Cleanup

```bash
cd monitoring
docker-compose down -v  # removes all data volumes
```

## Validator Monitoring

Prometheus scrapes metrics from 5 validators:

* `validator_1`: [http://localhost:8000/metrics](http://localhost:8000/metrics)
* `validator_2`: [http://localhost:8001/metrics](http://localhost:8001/metrics)
* `validator_3`: [http://localhost:8002/metrics](http://localhost:8002/metrics)
* `validator_4`: [http://localhost:8003/metrics](http://localhost:8003/metrics)
* `validator_5`: [http://localhost:8004/metrics](http://localhost:8004/metrics)

Scrape interval: **10 seconds**

## Dashboard Panels

1. **Block Height** — current blockchain height
2. **Total Transactions** — total number of transactions
3. **Mempool Size** — current mempool size
4. **Active Validators** — number of active validators
5. **Block Production Rate** — blocks per minute
6. **Transaction Throughput** — transactions per second (TPS)
7. **Mempool Over Time** — mempool dynamics
8. **Validator Uptime Score** — validator uptime metrics
9. **Token Economics** — token supply, burned, and minted
10. **Validator Voting Power** — voting power distribution
11. **Validator Performance** — table with per-validator metrics

## Health Checks

```bash
# Verify validator metrics endpoint
curl http://localhost:8000/metrics

# Verify Prometheus target discovery
curl http://localhost:9090/api/v1/targets

# Logs
docker logs computechain-prometheus
docker logs computechain-grafana
```
