# ComputeChain v3 monitoring

Prometheus + Grafana для CometBFT devnet. Старые FastAPI endpoints
на 8000–8004 и legacy performance/PoC-метрики не используются.

## Текущий стенд: multisite WAN fleet

Grafana сейчас показывает **cpc-multisite-devnet-1**: четыре валидатора и три full
nodes в трёх локациях. Открыть [WAN dashboard](http://192.168.0.100:3000/d/computechain-fleet).
Адрес, login `admin`, прежний пароль и Grafana/Prometheus volumes сохранены.
Каталог `.runtime/comet-staking-devnet/monitoring/` сохранён только ради существующих
volumes/учётной записи; это НЕ означает, что Grafana продолжает наблюдать старую сеть.
Explorer/website тоже показывают WAN chain через отдельный full-a1 observer;
прежний индекс локальной цепочки сохранён отдельно.

- Все семь источников: node ID/chain ID/genesis, доступность, высота, catch-up,
  block age, native power; сравнение общего block/AppHash. Без независимого light proof.
- Пять локальных узлов: native Comet metrics, mempool и read-only ABCI state.
  Два удалённых: существующие pinned TLS readers27626/27636, только read allowlist.
  Удалённые CPU/RAM, mempool, peers и ABCI не показываются как измеренные.
- TPS берётся с validator-a1; supply/stake с full-a1. Реплики не суммируются.
  Locations/machines — объявленные failure domains, НЕ доказательство независимости.
- Шесть Prometheus alert rules: node unavailable, stalled finality, height lag,
  common block conflict, missing witnesses, stale collector. Видны в dashboard и
  [Prometheus alerts](http://192.168.0.100:9090/alerts); Telegram/email не подключены.

Управление с корня workspace, БЕЗ рестарта chain:

```bash
.tools/blockchain-venv/bin/python monitoring/stack.py up --dir .runtime/comet-staking-devnet
.tools/blockchain-venv/bin/python monitoring/stack.py status --dir .runtime/comet-staking-devnet
systemctl status cpc-fleet-observer.service
```

Loopback observer27674 poll15s,4workers, CPU10% одного core/MemoryMax96MiB; locked
cpm-fleet sees only public config/CA/genesis/runtime, no keys/node DBs. Никаких SSH
паролей/ключей или remote host changes. Native local scrape5s, fleet scrape15s.
Loopback — граница хоста, не изоляция от других локальных процессов; allowlist
сборщика не заменяет production process-to-process RPC authentication.
Сохраняемый fleet-selection.json с SHA предотвращает возврат к старой цепочке
при `start_test.sh monitoring-up`. Old-chain dashboard отдельно помечен historical.
Существующий `cleanup.sh` может остановить общий monitoring Compose, но не новую цепь.

Public config: `/root/computechain-node/fleet-monitoring/fleet.json`; trust/leaf
pins из утверждённого bootstrap profile. CA/leaf rotation пока ручная; после
истечения сертификата remote up=0, а не last-good success. История индексов, wallets
и validator keys не читаются. Evidence: `.runtime/multisite-live/monitoring/verification.json`.
Новый observer устанавливается явно `install_fleet.py --root HOME/computechain-node
--config APPROVED_PUBLIC_JSON --config-sha256 APPROVED_SHA --port FREE_LOOPBACK_PORT`;
повторная установка/upgrade требует отдельной проверки, не overwrite live services.

## Отдельный исторический локальный devnet (reference)

Из `/root/computechain/computechain`:

```bash
./start_test.sh                         # 4 validators + full node + monitoring
./start_test.sh status
./start_test.sh load --mode medium --duration 60
./start_test.sh load-stop               # остановить только нагрузку
./cleanup.sh                           # остановить весь этот стенд, сохранить данные
```

Нужны Linux, работающий Docker Engine + Compose, соседний репозиторий monitoring
и локальные Python/CometBFT зависимости. Установка только chain tools:
`python3 scripts/setup_comet.py`. На текущем хосте всё установлено.

Вариант без Docker: `./start_test.sh up --no-monitoring`.
Мониторинг отдельно: `./start_test.sh monitoring-up`,
`monitoring-status`, `monitoring-down`. При запуске из этого репозитория:
`../computechain/start_test.sh monitoring-up`.

Повторный `up` сохраняет рабочие процессы и данные. `cleanup.sh` больше не
делает reset, не удаляет ключи/логи/данные и не использует глобальный pkill.
Monitoring volumes сохраняются; не использовать `down -v`.

## Доступ к интерфейсам

- Grafana: [http://192.168.0.100:3000/d/computechain-v2](http://192.168.0.100:3000/d/computechain-v2).
- Prometheus: [http://192.168.0.100:9090](http://192.168.0.100:9090).
- Scrape targets: [http://192.168.0.100:9090/targets](http://192.168.0.100:9090/targets).

Логин Grafana — `admin`. Случайный пароль создаётся один раз, а не admin/admin.
Файл: `/root/computechain/.runtime/comet-staking-devnet/monitoring/monitoring.env`, mode 0600.
Показать пароль себе на хосте:

```bash
sed -n 's/^GRAFANA_ADMIN_PASSWORD=//p' ../.runtime/comet-staking-devnet/monitoring/monitoring.env
```

Не публиковать этот файл и не отправлять его в Git. При restart используется
тот же пароль и те же volumes. Изменение файла само по себе не меняет пароль
существующего пользователя в Grafana DB.

Мониторы открываются напрямую с другой машины в LAN, без SSH tunnel. По умолчанию
определяется и сохраняется private LAN IPv4 хоста; можно задать адрес явно:

```bash
./start_test.sh monitoring-up --monitoring-host 192.168.0.100
```

Bind — конкретный LAN-адрес, не 0.0.0.0. Для возврата к host-only доступу можно
задать `--monitoring-host 127.0.0.1`. Grafana требует пароль, Prometheus не имеет
LAN-аутентификации: доступ рассчитан на доверенную сеть. NAT не защищает от её
клиентов; не пробрасывать HTTP-порты в Интернет. ABCI, RPC, P2P и exporters
блокчейна остаются локальными и не открываются вместе с мониторами.

## Что собирается

- Native CometBFT метрики 4 validators + full node: consensus height, committed
  TX counter, block intervals, mempool, peer count.
- Read-only exporter: engine/application height, catch-up, RPC/app availability,
  block age, account count, supply/burn и статистика нагрузочного запуска.
  Native voting power, bonded/unbonding CPC и scheduled validator count отражают
  динамический staking. Роль не определяется номером ноды; dashboard UID
  `computechain-v2` сохранён для совместимости ссылок. Float метрики — визуализация,
  не расчёт денежных сумм в ledger.
- Статистика генератора: submitted, confirmed, CheckTx rejected, execution failed,
  RPC errors, unresolved, target/observed TPS, observed p50/p95/p99 confirmation latency.
  CheckTx admission не считается финализацией.
- Prometheus self metrics.

Всего 7 scrape targets: 5 native endpoints + exporter + Prometheus.
Scrape/poll interval — 5 секунд. Finalized TPS берётся с **одной** ноды (node0),
иначе суммирование пяти реплик умножило бы результат на пять.
Latency — время от отправки до наблюдаемого успешного commit, включает polling;
quantiles вычисляются по последним 10000 подтверждениям, не по всей истории.

Default native ports: 28603/28613/28623/28633/28643.
Exporter — 28604. `stack.py` генерирует конфиг для реального base-port:
`prometheus/prometheus.yml` — только reference, не active runtime config.
Если нода недоступна, exporter показывает up=0 и не рисует выдуманную высоту.
CPC в графиках — approximate floating-point presentation; ledger хранит целые
базовые единицы и является источником финансовых данных.

## Свой каталог и порты

```bash
../computechain/start_test.sh up \
  --dir /root/computechain/.runtime/devnet-second --base-port 30600 \
  --prometheus-port 19090 --grafana-port 13000
../computechain/start_test.sh status --dir /root/computechain/.runtime/devnet-second
../computechain/cleanup.sh --dir /root/computechain/.runtime/devnet-second
```

Base-port задаётся при первом init; существующая сеть использует сохранённый
network.json. Monitoring ports фиксируются при первой настройке данного каталога;
launcher впоследствии подхватывает их автоматически. Выбирайте свободные диапазоны.
Другой --dir создаёт другой Compose project и отдельные volumes.

## Хранение и диагностика

Все runtime файлы находятся вне Git:
`.runtime/comet-staking-devnet/`, `monitoring/monitoring.env`,
`monitoring/prometheus.json`, `monitoring/datasources/prometheus.yaml`.
Логи: `app0.log`…`app4.log`, `engine0.log`…`engine4.log`,
`exporter.log`, `load.log`. Нагрузка: `load-latest.json` и per-run JSON.
Prometheus retention — 7 дней. Node history/signing state автоматически не стираются.

```bash
../computechain/start_test.sh monitoring-status
python3 stack.py logs --dir /root/computechain/.runtime/comet-staking-devnet
curl -fsS http://127.0.0.1:28604/metrics
curl -fsS http://192.168.0.100:9090/api/v1/targets
```

Если monitoring-up не прошёл, ноды могут оставаться запущенными: смотрите status,
устраните причину и повторите monitoring-up либо остановите cleanup.sh.
Не перезапускайте чужие контейнеры/процессы и не очищайте Docker volumes.

## Проверенная конфигурация

Pinned images с digest: Prometheus 3.15.0, Grafana 13.2.3.
Источники версий: [Prometheus release](https://github.com/prometheus/prometheus/releases/tag/v3.15.0),
[Grafana release](https://github.com/grafana/grafana/releases/tag/v13.2.3).
Используется [Linux host networking](https://docs.docker.com/engine/network/drivers/host/):
позволяет scrape loopback endpoints без публичного bind blockchain;
host-network containers не являются изолированной границей доверия.
Этот стек — локальный devnet, не production deployment.

Проверены настоящие scrape, provisioning/dashboard API, signed load и stop/restart.
Unit tests: из blockchain repo
`./run_tests.sh tests/test_devnet_tools.py ../monitoring/tests -q`.
