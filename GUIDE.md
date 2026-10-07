# Руководство пользователя: Agentic SRE Testbed

Полное описание: как запустить стенд, работать с приложением, смотреть телеметрию, ломать систему фолтами,
запускать эксперименты и возвращать всё в исходное состояние.

Справочники для разработчиков: `README.md` (кратко, по-английски) и `docs/kb/*.md` (детали по темам).

---

## Содержание

1. [Что это такое](#1-что-это-такое)
2. [Что нужно установить](#2-что-нужно-установить)
3. [Первый запуск за 3 минуты](#3-первый-запуск-за-3-минуты)
4. [Устройство системы и адреса](#4-устройство-системы-и-адреса)
5. [Работа с приложением (API)](#5-работа-с-приложением-api)
6. [Телеметрия: Grafana, логи, метрики, трейсы](#6-телеметрия-grafana-логи-метрики-трейсы)
7. [Нагрузка](#7-нагрузка)
8. [Фолты: как ломать систему](#8-фолты-как-ломать-систему)
9. [Эксперименты: воспроизводимые инциденты](#9-эксперименты-воспроизводимые-инциденты)
10. [Приёмка всех сценариев (acceptance)](#10-приёмка-всех-сценариев-acceptance)
11. [Тесты](#11-тесты)
12. [Пример: расследование инцидента от начала до конца](#12-пример-расследование-инцидента-от-начала-до-конца)
13. [Настройки (.env)](#13-настройки-env)
14. [Очистка, сброс, остановка](#14-очистка-сброс-остановка)
15. [Если что-то пошло не так](#15-если-что-то-пошло-не-так)
16. [Шпаргалка команд](#16-шпаргалка-команд)

---

## 1. Что это такое

Небольшая распределённая система, похожая на продакшен: интернет-магазин из нескольких сервисов. Её
**специально и контролируемо ломают**, чтобы тренировать и оценивать SRE-агентов: агент видит только
симптомы и телеметрию и должен найти причину.

Три плоскости, которые важно различать:

| Плоскость | Что в ней | Кто её видит |
|---|---|---|
| **Приложение** | nginx, gateway, auth, order, payment, PostgreSQL, Redis | клиенты (вы через `curl`, генератор нагрузки) |
| **Диагностическая** | Prometheus, Loki, Tempo, Grafana | тот, кто расследует (вы, будущий агент) |
| **Управляющая (control plane)** | fault-injector, experiment-runner, portal (Control Center) | только «организатор»: знает правду о том, что сломано |

Главное правило: **управляющая плоскость невидима для диагностической.** Логи injector, runner и портала не
попадают в Loki, их нет в Prometheus и Tempo. В телеметрии видны только *симптомы*, а не «фолт такой-то
включён». Поэтому расследование здесь похоже на настоящее.

---

## 2. Что нужно установить

| Что | Зачем | Проверка |
|---|---|---|
| Docker Desktop (или Docker Engine) с Compose v2 | всё работает в контейнерах | `docker compose version` |
| GNU Make | все команды — цели Makefile | `make --version` |
| curl 7.76+ | Makefile ходит в API | `curl --version` |
| python3 | `make smoke`, `make acceptance`, красивый вывод JSON | `python3 --version` |
| jq (необязательно) | удобно доставать токен в примерах | `jq --version` |

Ресурсы: рекомендуется выделить Docker **4 CPU и 6 ГБ RAM** (ориентир, а не измеренный минимум). Стенд
поднимает 18 контейнеров, а каждому сервису приложения выдаётся 0.5 CPU и 256 МБ.

Python-зависимости на хост ставить не нужно: тесты и генератор нагрузки работают в контейнере.

---

## 3. Первый запуск за 3 минуты

```bash
git clone https://github.com/Michael2701/Agentic_SRE_Testbed.git
cd Agentic_SRE_Testbed

make up      # сборка и запуск; ждёт, пока все контейнеры станут healthy (~1–2 мин в первый раз)
make smoke   # проверка за ~5 секунд: заказ проходит, фолтов нет, телеметрия и портал доступны
make open    # открыть Control Center в браузере
```

`make smoke` печатает список проверок, ожидаемый результат — `ok` в каждой строке:

```text
ok   app: ready, login, order 201
ok   no active faults
ok   no leftover redeploy containers
ok   prometheus targets up
ok   loki reachable
ok   tempo reachable
ok   portal http://localhost:8000: every tool reachable
```

После этого откройте **один адрес** (или выполните `make open`):

## 👉 http://localhost:8000 — Control Center

Это единый интерфейс стенда: всё делается в нём, без перехода в другие системы. Работает на macOS, Linux и
Windows, в любом браузере, и без интернета (библиотеки лежат в репозитории).

| Вкладка | Что в ней |
|---|---|
| **Overview** (обзор) | p95 / 5xx / нагрузка с графиками; карта сервисов, где цвет ребра — его здоровье, а подпись — p95; домены application / database / redis / payment: NORMAL или DEGRADED; таблица сервисов |
| **Faults** (фолты) | форма включения: тип → цель → параметры с подсказками по умолчаниям и лимитам (строится из каталога injector'а); кнопки **Validate** и **Inject**; активные фолты с кнопкой **Remove**, **Remove all**, **Clean up**, история |
| **Experiments** (эксперименты) | все сценарии с кнопкой **▶ Run** (по умолчанию «short run» ~1.5 мин); живые фазы, графики p95 и 5xx с отметками inject / onset / remove, вердикт, ground truth; **Abort**; история; **🎲 Random, blind** |
| **Logs & traces** (логи и трейсы) | поиск по request id, trace id или тексту с фильтрами по сервису, уровню и времени; клик **trace ↗** у строки открывает водопад спанов рядом |
| **Dashboards** (дашборды) | все 5 дашбордов Grafana внутри Control Center, переключение кнопками |

Вверху всегда видно, сколько фолтов активно и идёт ли эксперимент, плюс переключатель **👁 investigation mode**
(режим расследования). Он прячет всё, что выдаёт причину: активные фолты, имена сценариев, ground truth. Включите
его, запустите **🎲 Random, blind** и найдите причину по вкладкам Overview, Logs & traces и Dashboards. Ответ откроется кнопкой
**👁 Reveal the answer** у эксперимента.

**Клиенты приложения** (curl, генератор нагрузки) ходят на `http://localhost:8080`: это публичный вход
nginx, и именно его запросы видны в телеметрии.

Control Center работает в отдельном контейнере `portal`, а не в nginx приложения. Поэтому запросы к
управляющим API не попадают в логи и трейсы стенда. Порт меняется в `.env` (`PORTAL_PORT`).

<details><summary>Полные интерфейсы и API на том же адресе (если нужно больше, чем в Control Center)</summary>

| Путь | Что |
|---|---|
| `/grafana/` | полная Grafana: Explore, редактирование дашбордов |
| `/prometheus/` | Prometheus UI |
| `/faults/docs` | Swagger API фолтов |
| `/experiments/docs` | Swagger API экспериментов |
| `/app/docs` | Swagger API приложения |
</details>

<details><summary>Прямые порты (для скриптов; в браузере используйте портал)</summary>

| Что | Порт |
|---|---|
| приложение | 8080 |
| Grafana (API; UI открывайте через `/grafana/`, ссылки в нём ведут на портал) | 3000 |
| Prometheus (API) | 9090 |
| fault-injector | 8090 |
| experiment-runner | 8091 |

Все, кроме приложения, слушают только 127.0.0.1.
</details>

---

## 4. Устройство системы и адреса

```text
                         Redis
                           ↑
Client → nginx → gateway → auth
                    │
                    └──→ order → PostgreSQL
                           │
                           ↓
                        payment
```

| Сервис | Роль |
|---|---|
| **nginx** | единственная публичная точка входа, проксирует всё в gateway, начинает трейс |
| **gateway** | проверяет токен через auth и передаёт запрос в order |
| **auth** | фейковый логин; выдаёт токены и хранит их в Redis с TTL |
| **order** | создаёт заказ в PostgreSQL (`pending`), списывает деньги через payment, ставит `paid` или `payment_failed` |
| **payment** | симулятор платежей, всегда одобряет (если его не сломали) |
| **postgres / redis** | хранилища |
| **prometheus / loki / tempo / alloy / grafana** | телеметрия: метрики, логи, трейсы, сбор логов, дашборды |
| **fault-injector** | ломает и чинит систему (control plane) |
| **experiment-runner** | прогоняет сценарии целиком (control plane) |
| **portal** | Control Center на `localhost:8000`: единый интерфейс для людей (control plane, невидим для телеметрии) |

**Тайм-ауты вложены:** order ждёт payment 5 с, gateway ждёт order 8 с. Поэтому order всегда успевает
ответить (и записать исход заказа) раньше, чем gateway сдастся.

---

## 5. Работа с приложением (API)

Демо-пользователи: `alice` / `alice` и `bob` / `bob`. Аутентификация фейковая, это сделано намеренно.

### 5.1. Получить токен

```bash
TOKEN=$(curl -s -XPOST localhost:8080/login -H 'content-type: application/json' \
  -d '{"username":"alice","password":"alice"}' | jq -r .access_token)
echo $TOKEN
```

Ответ на логин: `{"access_token": "...", "token_type": "bearer", "expires_in": 3600}`.

### 5.2. Создать заказ

```bash
curl -s -XPOST localhost:8080/orders \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"item":"book","quantity":1,"amount_cents":1500}'
```

| Поле | Правило |
|---|---|
| `item` | непустая строка |
| `quantity` | целое, от 1 до 2147483647 |
| `amount_cents` | целое, от 1 до 2147483647 |
| `currency` | 3 буквы, по умолчанию `USD` |

### 5.3. Прочитать заказ

```bash
curl -s localhost:8080/orders/<id-заказа> -H "Authorization: Bearer $TOKEN"
```

Чужой заказ возвращает 404: каждый пользователь видит только свои заказы.

### 5.4. Коды ответов

| Код | Когда |
|---|---|
| 201 | заказ создан и оплачен (`status: paid`) |
| 400 | тело запроса — не JSON |
| 401 | нет токена, токен неверный или истёк |
| 404 | заказа нет (или он чужой) |
| 422 | невалидные поля |
| 500 | внутренняя ошибка сервиса (например, БД отказала в соединении) |
| 502 | зависимость упала; для заказа — платёж не прошёл, заказ сохранён как `payment_failed` |
| 503 | БД не ответила за 10 с, или оплата прошла, но статус заказа записать не удалось |
| 504 | gateway не дождался ответа (8 с) |

### 5.5. Идентификаторы для расследования

Каждый ответ содержит два заголовка:

- `X-Request-ID` — ID запроса, он есть в каждой строке лога каждого сервиса на пути запроса. Можно
  передать свой (`-H 'X-Request-ID: my-test-1'`; допустимы буквы, цифры, `.`, `_`, `-`, до 128 символов).
  Если ID невалидный, nginx заменит его своим.
- `X-Trace-ID` — ID трейса в Tempo.

```bash
curl -si -XPOST localhost:8080/orders -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -H 'X-Request-ID: demo-42' \
  -d '{"item":"book","quantity":1,"amount_cents":1500}' | grep -iE 'x-request-id|x-trace-id'
```

---

## 6. Телеметрия: Grafana, логи, метрики, трейсы

### 6.1. Дашборды

Control Center → вкладка **Dashboards** (там все пять, переключение кнопками; полная Grafana — `/grafana/`):

| Дашборд | Что показывает | Когда смотреть |
|---|---|---|
| **Service Overview** (домашний) | RED (rate/errors/duration) по сервисам, латентность пути заказа, бизнес-счётчики, CPU/троттлинг/память/PSI контейнеров, версии и uptime | первым делом: «что болит?» |
| **Dependencies** | каждое ребро вызовов (gateway→auth, order→payment, order→postgres, auth→redis …): латентность, ошибки, исход (`success/timeout/error/cancelled`); соединения PostgreSQL по ролям, блокировки, Redis | «какая зависимость тормозит или падает?» |
| **Logs** | логи с фильтрами по сервису, уровню и request_id | «что именно произошло с этим запросом?» |
| **Traces** | карта сервисов, недавние и медленные трейсы `POST /orders`, метрики по рёбрам из спанов | «где именно теряется время?» |
| **Diagnostic domains** | симптом заказа + сигналы по доменам: application, database, redis, payment | быстрая оценка «какой домен деградировал» |

> На **Diagnostic domains** намеренно нет домена «прокси» (nginx). На этом построены «model-breaking»
> сценарии: все домены зелёные, а проблема в nginx, и видна она только в его логах и трейсах.

### 6.2. Логи (Loki)

Каждая строка лога — JSON: `ts, level, service, msg, request_id, trace_id, span_id, version` плюс поля
события. Важные события: `request` (одна строка access-лога на запрос), `order_created`,
`order_payment_failed`, `dependency_call_failed`, `login_failed`, `database_timeout`.

Проще всего — Control Center → вкладка **Logs & traces**: вставьте request id, trace id или текст. Для
произвольных LogQL-запросов: `/grafana/` → **Explore** → источник **Loki**, примеры:

```logql
{service=~".+"} |= "demo-42"                          # весь путь одного запроса по request_id
{service="order"} | json | level="error"              # ошибки order
{service=~".+"} |= "dependency_call_failed"           # все неудачные вызовы зависимостей
{service="nginx"} | json | request_time_s > 1         # медленные запросы на входе
```

Метки: `service`, `container`, `level`. `request_id` — не метка, ищите его через `|= "..."` или
`| json | request_id="..."`.

### 6.3. Метрики (Prometheus)

У каждого сервиса приложения есть `/metrics`; имя сервиса — метка `job`.

| Метрика | Что это |
|---|---|
| `http_requests_total{method,route,status}` | входящие запросы |
| `http_request_duration_seconds` | длительность входящих запросов (гистограмма до 30 с) |
| `dependency_requests_total{dependency,operation,outcome}` | исходящие вызовы и их исход |
| `dependency_request_duration_seconds` | длительность исходящих вызовов |
| `orders_total{status}`, `payments_total`, `logins_total`, `token_validations_total` | бизнес-счётчики |
| `app_build_info{version}` | запущенная версия (новая серия = был деплой) |
| `container_cpu_throttled_seconds_total`, `container_memory_usage_bytes`, PSI | ресурсы контейнера |
| `pg_*`, `redis_*`, `nginx_*` | экспортеры инфраструктуры |

Примеры (http://localhost:8000/prometheus/ или Grafana → Explore → Prometheus):

```promql
# p95 создания заказа на gateway
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="gateway",route="/orders",method="POST"}[1m])))

# доля 5xx по сервисам
sum by (job) (rate(http_requests_total{status=~"5.."}[1m])) / sum by (job) (rate(http_requests_total[1m]))

# неудачные вызовы зависимостей по рёбрам
sum by (job, dependency, outcome) (rate(dependency_requests_total{outcome!="success"}[1m]))
```

### 6.4. Трейсы (Tempo)

Каждый запрос трассируется целиком (100% сэмплинг): `nginx → gateway → auth → Redis` и
`→ order → PostgreSQL / payment`. Спаны содержат `user.id`, `order.id`, `order.status`, `payment.id`.

- Control Center → **Logs & traces** → вставьте `X-Trace-ID` (или нажмите **trace ↗** у строки лога), и справа
  появится водопад спанов.
- Полный просмотр: `/grafana/` → **Explore** → **Tempo**.
- Или дашборд **Traces** → медленные трейсы.
- Из строки лога в Loki можно перейти к её трейсу, а из спана — к логам сервиса.

Хранение: метрики, логи и трейсы хранятся **48 часов**.

---

## 7. Нагрузка

Чтобы на графиках было что смотреть, нужен поток запросов:

```bash
make load                       # 5 запросов/с в течение 60 с
make load RATE=10 DURATION=300  # 10 запросов/с в течение 5 минут
make load RATE=5 DURATION=600 & # в фоне, пока вы ломаете систему
```

Смесь запросов как у реального клиента: создание заказов, чтение своих заказов, немного неудачных логинов
(фоновый шум).

---

## 8. Фолты: как ломать систему

### 8.1. Основные команды

В браузере: Control Center → вкладка **Faults** (форма, списки, кнопки **Remove**). То же из терминала:

```bash
make fault TYPE=payment-latency                              # включить фолт с параметрами по умолчанию
make fault TYPE=payment-latency PARAMS='{"latency_ms":2500}' # со своими параметрами
make fault TYPE=network-latency TARGET=order PARAMS='{"delay_ms":300,"peer":"payment"}'
make fault TYPE=cpu-saturation TARGET=order EXP=my-test-1    # пометить своим experiment_id

make faults    # список активных фолтов
make recover   # снять ВСЕ активные фолты (и дождаться, пока сервисы поднимутся)
```

- `TYPE` пишется через дефис (`payment-latency`), Makefile сам превращает его в `payment_latency`.
- `TARGET` нужен, если у типа несколько возможных целей (см. таблицу ниже).
- `PARAMS` — JSON; что не указано, берётся по умолчанию.
- Если API ответил ошибкой (409, 422, 502), `make` завершится с ненулевым кодом и покажет тело ответа.

### 8.2. Все 22 типа фолтов

Механизмы настоящие, где это возможно: остановка и пауза контейнеров, `tc`/`iptables` в сетевом
пространстве, триггер в PostgreSQL, редеплой контейнера с другой конфигурацией и т. п.

**Приложение и зависимости**

| TYPE | Цели | Параметры (по умолчанию) | Что происходит |
|---|---|---|---|
| `payment-latency` | payment | `latency_ms` (2000), `jitter_ms` (0), `probability` (1) | payment отвечает медленно |
| `payment-error` | payment | `status_code` (500), `probability` (1) | payment возвращает 5xx |
| `intermittent-errors` | auth, order, payment | `error_rate` (0.3), `status_code` (500) | часть запросов получает 5xx |
| `service-unavailable` | auth, order, payment | — | контейнер остановлен (connection refused) |
| `dependency-timeout` | auth, order, payment | — | контейнер на паузе (соединение принимается, ответа нет) |
| `redis-unavailable` | redis | — | Redis остановлен |

**Ресурсы**

| TYPE | Цели | Параметры (по умолчанию) | Что происходит |
|---|---|---|---|
| `cpu-saturation` | gateway, auth, order, payment | `workers` 1–32 (2) | процессы-«печки» внутри контейнера съедают его квоту CPU |
| `cpu-limit` | то же | `cpus` 0.01–4 (0.1) | квота CPU контейнера понижена «на лету», без рестарта |
| `memory-pressure` | то же | `mb` 16–2048 (200) | процесс занимает память внутри контейнера (лимит 256 МБ) |

**Базы данных**

| TYPE | Цели | Параметры (по умолчанию) | Что происходит |
|---|---|---|---|
| `db-slow-query` | postgres | `delay_ms` (обязателен), `operations` (`["insert","update"]`) | триггер с `pg_sleep` на запись в `orders` |
| `db-connection-exhaustion` | postgres | — | другая роль занимает все обычные слоты соединений |
| `db-lock-contention` | postgres | `hold_ms` (1500), `interval_ms` (2000) | периодический `LOCK TABLE orders IN SHARE MODE` |
| `redis-latency` | redis | `pause_ms` (обязателен), `interval_ms` (1000) | периодический `CLIENT PAUSE` |

**Сеть**

| TYPE | Цели | Параметры (по умолчанию) | Что происходит |
|---|---|---|---|
| `network-latency` | сервисы приложения, postgres, redis, nginx | `delay_ms` (обязателен), `jitter_ms` (0), `peer` | задержка `tc netem` на исходящем трафике (только к `peer`, если он задан) |
| `packet-loss` | то же | `loss_percent` (20), `peer` | потеря пакетов (в основном бьёт по хвосту латентности) |
| `connection-failure` | gateway, auth, order | `peer` (обязателен), `mode` `reject`/`drop` (reject) | `iptables`: reset (быстрый отказ) или drop (тайм-аут) |

**Конфигурация и деплой** (контейнер пересоздаётся, как при настоящем релизе)

| TYPE | Цели | Параметры (по умолчанию) | Что происходит |
|---|---|---|---|
| `incorrect-endpoint` | gateway, order, auth | `dependency` (обязателен), `endpoint` (опечатка в имени хоста) | неверный URL зависимости |
| `incorrect-timeout` | gateway, order | `timeout_ms` (5) | слишком короткий HTTP-тайм-аут |
| `bad-configuration` | gateway, order, auth | `settings` (обязателен), например `{"DB_POOL_MAX_SIZE":1}`, `{"TOKEN_TTL_SECONDS":2}` | неудачные значения переменных окружения |
| `bad-deployment` | auth, order, payment | `version` (1.1.0), `defect` `crash`/`errors`/`slow`/`none` (errors), `error_rate` (0.5), `latency_ms` (800) | новая версия падает в цикле, отдаёт 5xx, тормозит или безвредна (`none` — приманка) |

**Прокси (nginx)**

| TYPE | Цели | Параметры (по умолчанию) | Что происходит |
|---|---|---|---|
| `proxy-rate-limit` | nginx | `rate_rps` (8), `burst` (20) | слишком строгий `limit_req`: очередь, сверх лимита — 503 |
| `proxy-bandwidth-limit` | nginx | `bytes_per_second` (100) | `limit_rate` на ответ («забыли k»): медленно, но без ошибок |

Полный список допустимых значений — в `services/fault-injector/app/catalog.py`. Проверить параметры, ничего
не применяя:

```bash
curl -s -XPOST localhost:8000/faults/faults/validate -H 'content-type: application/json' \
  -d '{"type":"db_slow_query","parameters":{"delay_ms":300}}' | python3 -m json.tool
```

### 8.3. Как ведут себя фолты

- **Хранятся и поддерживаются.** Фолты записаны в SQLite и переприменяются каждые 2 с: они переживают
  рестарт injector'а и рестарт самой цели.
- **Конфликты (409).** Нельзя одновременно:
  - два фолта одного типа на одну цель;
  - на одном контейнере больше одного «состояния контейнера» (стоп, пауза, редеплой, cpu-limit);
  - на одной цели больше одного сетевого фолта.

  Остальные комбинации допустимы, например `cpu-saturation` вместе с `dependency-timeout` на одном
  контейнере.
- **Если фолт не применился (502).** Injector откатывает частично сделанное, фолт получает `state: failed`
  и `error` с причиной. Если не удался даже откат, ставится `needs_cleanup: true`: такой фолт снимается
  `make recover` (или `DELETE`), а до тех пор блокирует конфликтующие.
- **Поле `error` у активного фолта** означает, что фолт сейчас не удаётся поддерживать (например, `enforce
  failed: …`). Остальные фолты при этом продолжают работать. Когда проблема уйдёт, ошибка очистится сама.
- **Снятие.** `make recover` пробует снять каждый фолт и один раз повторяет неудачные (снятие одного может
  зависеть от другого). Если что-то так и не снялось, вы получите 502 со статусом по каждому фолту.
- **Кратковременные 502 после редеплоя** (включение или снятие фолтов конфигурации и деплоя) нормальны:
  клиенты пару секунд держат старое соединение или DNS-запись.

### 8.4. API напрямую

Базовый адрес: `http://localhost:8000/faults` (через портал; путь ниже дописывается к нему), в браузере —
http://localhost:8000/faults/docs.

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/faults` | `{type, target?, parameters?, experiment_id?}` → 201 |
| POST | `/faults/validate` | проверить и дополнить параметры, ничего не применяя |
| GET | `/faults[?state=active\|removed\|failed]` | список фолтов (снятые остаются в истории) |
| GET | `/faults/{id}` | один фолт |
| DELETE | `/faults/{id}` | снять один фолт |
| DELETE | `/faults` | снять все |

```bash
curl -s -XPOST localhost:8000/faults/faults -H 'content-type: application/json' \
  -d '{"type":"db_lock_contention","parameters":{"hold_ms":1500,"interval_ms":2000}}'
curl -s -XDELETE localhost:8000/faults/faults/flt-xxxxxxxxxxxx
```

### 8.5. Похожие симптомы — разные причины

На этом построен смысл стенда. Подробная таблица «симптом → возможные причины → чем отличить» —
`docs/kb/faults.md`, раздел *Symptom → possible root causes*. Коротко:

| Симптом на `POST /orders` | Возможные причины |
|---|---|
| медленно, но 201 | payment-latency, db-slow-query, db-lock-contention, redis-latency, cpu-saturation, cpu-limit, network-latency, packet-loss (хвост), bad-deployment slow, проблемы прокси |
| 5xx | payment-error, intermittent-errors, db-connection-exhaustion, redis-unavailable, dependency-timeout, service-unavailable, connection-failure, incorrect-endpoint, incorrect-timeout, bad-deployment crash/errors |
| 401 | bad-configuration (короткий TTL токена) |
| снаружи ничего | memory-pressure; bad-configuration с маленьким пулом (пока БД не замедлится) |

---

## 9. Эксперименты: воспроизводимые инциденты

Эксперимент — это сценарий, прогнанный целиком под генерируемой нагрузкой:

```text
baseline → inject → observe → record → remove → recovery
(нагрузка идёт всё время; runner измеряет каждый запрос по фазам)
```

| Фаза | Что происходит |
|---|---|
| baseline | нормальная работа, замер эталона |
| inject | включаются фолты сценария |
| observe | наблюдение за инцидентом |
| record | подсчёт статистики, симптомов, сигналов по доменам |
| remove | снятие фолтов |
| recovery | ожидание окна без симптомов: система восстановилась? |

### 9.1. Запуск

В браузере: Control Center → вкладка **Experiments** → **▶ Run** у сценария. Фазы, графики и вердикт видны
вживую. То же из терминала:

```bash
make scenarios                            # список сценариев и их валидность
make experiment SCENARIO=payment-latency  # запустить и дождаться (~75 с), напечатать итог
make experiments                          # все записанные эксперименты (JSON, с ground truth)
```

Пример вывода (значения условные):

```text
exp-1  scenario=payment-latency  state=completed
ground truth: payment_latency on payment {"latency_ms": 1500, "jitter_ms": 0, "probability": 1.0}
baseline  p50=0.013s p95=0.025s errors=0% 401=0% rps=4.66
observe   p50=1.515s p95=1.526s errors=0% 401=0% rps=4.83
domains:  application=NORMAL  database=NORMAL  redis=NORMAL  payment=DEGRADED(server_p95,client_p95)
incident: onset=2026-10-07T10:15:32.104Z symptoms=['slow']
verdict: expected=slow observed=['slow'] expected_seen=True recovered=True recovery_seconds=0.0 evidence_matched=None
```

Как читать вывод:

- `ground truth` — что на самом деле сломали. **Это «ответ»; тому, кто расследует, его не показывают.**
- `baseline` / `observe` — клиентские метрики до и во время инцидента.
- `domains` — какие домены деградировали по метрикам Prometheus.
- `incident` — то, что получает расследующий: начало (onset — когда появились симптомы, а не когда
  включили фолт), конец и симптомы. **Причины здесь нет.**
- `verdict`:

  | Поле | Значение |
  |---|---|
  | `expected_seen` | ожидаемый симптом появился |
  | `recovered` | после снятия фолтов система восстановилась |
  | `evidence_matched` | картина по доменам совпала с ожидаемой |

Одновременно идёт только один эксперимент. Он не стартует, если есть активные фолты (409): сначала
`make recover`.

### 9.2. Сценарии

**Простые (одна причина)**

| Сценарий | Ожидаемый симптом | Что сломано |
|---|---|---|
| payment-latency | slow | payment отвечает медленно |
| payment-error | errors | половина платежей падает с 500 |
| db-slow-query | slow | запись в PostgreSQL 300 мс |
| redis-unavailable | errors | Redis лежит, валидация токенов не проходит |
| network-latency | slow | 300 мс на сети order → payment |
| cpu-saturation | slow | «печки» съедают CPU order |
| connection-failure | errors | соединения order → payment сбрасываются |
| bad-deployment | errors | новая версия payment роняет половину запросов |

**Сложные (M8)**

| Тип | Сценарии | В чём сложность |
|---|---|---|
| Одинаковый симптом | slow-payment, slow-db-query, slow-pool-exhaustion, slow-redis, slow-cpu, slow-network, slow-proxy-network, slow-proxy-config | у всех p95 заказа > 2 с при ответах 201 и одинаковой нагрузке; отличаются только улики |
| Ложная корреляция | deploy-then-payment | безвредный релиз order, а через 5 минут тормозит payment; откат релиза не помогает |
| Ломает модель | slow-proxy-network, slow-proxy-config | все домены NORMAL, проблема в nginx, видна только в его логах и трейсах |

Файлы сценариев: `experiments/scenarios/<имя>.json`. Свой сценарий — новый JSON-файл там же, поля как у
существующих (`docs/kb/experiments.md`).

### 9.3. Запуск с изменёнными параметрами (через API)

В Control Center короткий прогон включается галкой **short run**, а нагрузка задаётся полем **req/s**.
`make experiment` всегда берёт длительности из файла. Произвольные значения передаются через API в
`overrides`:

```bash
curl -s -XPOST 'localhost:8000/experiments/experiments?wait=true&format=text' -H 'content-type: application/json' -d '{
  "scenario": "slow-redis",
  "overrides": {
    "durations": {"baseline_s": 10, "observe_s": 20, "recovery_window_s": 5},
    "traffic": {"rate": 8}
  }
}'
```

| Ключ | Что меняет |
|---|---|
| `durations.baseline_s`, `durations.observe_s` | длительности фаз (3–600 с) |
| `durations.recovery_window_s`, `durations.recovery_timeout_s` | окно «здоровья» и общий лимит на восстановление |
| `traffic.rate` | запросов в секунду (до 50) |
| `traffic.max_in_flight` | сколько запросов может быть в полёте одновременно |
| `time_scale` | множитель отложенных фолтов, например 0.1 превращает 5 минут в 30 секунд |

### 9.4. Прервать эксперимент

```bash
curl -s 'localhost:8000/experiments/experiments?state=running' | python3 -m json.tool   # узнать id
curl -s -XDELETE localhost:8000/experiments/experiments/exp-7                        # прервать
```

Нагрузка останавливается, фолты эксперимента снимаются, `state` становится `aborted`. Если какой-то фолт
снять не удалось, это будет записано в `error`.

---

## 10. Приёмка всех сценариев (acceptance)

Прогоняет **все** сценарии подряд и печатает таблицу результатов.

```bash
make acceptance                                    # укороченные фазы, ~30 мин
make acceptance FULL=1                             # полные длительности из файлов, ~1 ч
make acceptance ROUNDS=2                           # каждый сценарий дважды: вердикты должны совпасть
make acceptance SCENARIOS="slow-redis slow-cpu"    # только выбранные
```

Сценарий проходит, если эксперимент завершился, ожидаемый симптом появился, система восстановилась и
картина по доменам совпала (где она задана). Код выхода 1, если хоть что-то не прошло.

> На ноутбуке длинные прогоны запускайте под `caffeinate -i make acceptance`, чтобы Mac не уснул.

---

## 11. Тесты

Тесты работают в контейнере против запущенного стенда (`make up` должен быть выполнен).

| Команда | Что проверяет | Время |
|---|---|---|
| `make test` | всё: integration → faults → experiments | ~17 мин |
| `make test-faults` | только фолты | ~7 мин |
| `make test-experiments` | только эксперименты (включая challenge-сценарии) | ~13 мин |
| `make test-challenges` | только сложные сценарии M8 | ~7–10 мин |

Перед стартом тесты сами снимают оставшиеся фолты. Если в этот момент идёт эксперимент, они откажутся
запускаться, чтобы не испортить чужой прогон. Во время `make test` не запускайте `make load`: один тест
проверяет точный прирост счётчика.

---

## 12. Пример: расследование инцидента от начала до конца

Цель: увидеть, как выглядит инцидент «изнутри», не подглядывая в ответ.

**Шаг 1. Чистый старт**

```bash
make smoke          # всё ok?
make faults         # должно быть []
```

**Шаг 2. Нагрузка в фоне**

```bash
make load RATE=5 DURATION=600 &
```

**Шаг 3. Кто-то другой ломает систему.** Попросите коллегу или сделайте сами, «не глядя». Проще всего —
Control Center: включите **👁 investigation mode**, затем Experiments → **🎲 Random, blind**. Сценарий и
фолты будут скрыты до кнопки **👁 Reveal the answer**. Или вручную, из терминала:

```bash
make fault TYPE=db-lock-contention
```

**Шаг 4. Расследование только по телеметрии**

1. Control Center → **Overview**: p95 `POST /orders` вырос? Ошибки есть? Медленно, но 201 — значит,
   «slow»-группа причин.
2. Там же блок **Domains**: какой домен DEGRADED? Здесь это будет database, а ребро `order → postgres` на карте станет оранжевым.
3. **Dashboards → Dependencies**: ребро `order → postgres` медленное, `order → payment` в норме. Панель блокировок
   PostgreSQL показывает периодические `ShareLock`.
4. **Logs & traces** → **trace ↗** у медленного запроса: время уходит в спаны `INSERT`/`UPDATE` PostgreSQL.
5. В логах ошибок нет, просто медленно.
6. Отличаем от похожих причин: `db-slow-query` даёт ровную задержку, а блокировка — периодические всплески
   (держится 1.5 с из каждых 2 с). Причина — lock contention на таблице `orders`.

**Шаг 5. Сверка с ответом и уборка**

```bash
make faults       # ground truth: db_lock_contention
make recover      # снять
make smoke        # убедиться, что всё вернулось
```

То же самое, но воспроизводимо и с автоматическим вердиктом, делает `make experiment SCENARIO=...`.

---

## 13. Настройки (.env)

Значения по умолчанию заданы в `docker-compose.yml`. Чтобы их поменять:

```bash
cp .env.example .env
# отредактировать .env, затем:
make up
```

`.env` читают **и compose, и Makefile**, поэтому порты из `.env` подхватятся во всех командах.

| Переменная | По умолчанию | Что это |
|---|---|---|
| `NGINX_PORT` | 8080 | порт приложения |
| `GRAFANA_PORT` | 3000 | Grafana |
| `PROMETHEUS_PORT` | 9090 | Prometheus |
| `PORTAL_PORT` | 8000 | Control Center: один адрес для всего |
| `FAULT_INJECTOR_PORT` | 8090 | API фолтов |
| `EXPERIMENT_RUNNER_PORT` | 8091 | API экспериментов |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | app / app / orders | суперпользователь и БД |
| `TOKEN_TTL_SECONDS` | 3600 | время жизни токена |
| `DEMO_USERS` | `alice:alice,bob:bob` | пользователи |
| `GATEWAY_HTTP_TIMEOUT_SECONDS` | 8 | сколько gateway ждёт auth/order |
| `ORDER_HTTP_TIMEOUT_SECONDS` | 5 | сколько order ждёт payment (должно быть меньше предыдущего) |
| `DB_POOL_MAX_SIZE` | 30 | пул соединений order |

---

## 14. Очистка, сброс, остановка

| Команда | Что делает | Данные |
|---|---|---|
| `make recover` | снимает все фолты | сохраняются |
| `make down` | останавливает контейнеры | сохраняются (тома остаются) |
| `make up` | запускает снова | активные фолты **вернутся**, если не сняли их до `down` |
| `make reset` | снимает фолты → удаляет **всё** (БД, историю фолтов и экспериментов, телеметрию) → запускает → smoke | **удаляются**, ~1 мин |
| `make ps` | статус контейнеров | — |
| `make logs` | поток логов всех контейнеров (включая control plane) | — |

> Перед `make down` лучше сделать `make recover`. Фолты хранятся в томе и после `make up` снова включатся.

---

## 15. Если что-то пошло не так

| Проблема | Что делать |
|---|---|
| `make smoke` не ok / что-то «осталось» | `make recover`, затем `make smoke`. Не помогло — `make reset` |
| `make experiment` → 409 | идёт другой эксперимент или есть активные фолты: `make faults`, `make recover` |
| `make fault` → 409 | конфликт с активным фолтом (см. 8.3); сообщение называет, с каким |
| `make fault` → 422 | неверный тип, цель или параметры; сообщение объясняет, что не так |
| `make fault` → 502 | фолт не удалось применить; причина в `error`, частичные изменения откатаны |
| 502 несколько секунд после редеплой-фолта | нормально (старые соединения и DNS), подождите |
| `make up` долго ждёт | Loki и Tempo после старта ~15 с отвечают «not ready», это нормально |
| `nginx reload failed` у прокси-фолтов | nginx должен видеть актуальный `nginx/nginx.conf` (монтируется каталог, а не файл); `make reset` |
| `localhost:8000` → `404 Not Found` (страница nginx) | папку `portal/` пересоздали на диске (например, `git checkout` ветки без неё), и контейнер видит пустую старую папку: `docker compose up -d --force-recreate portal` |
| `{"detail":"Not Found"}` в браузере | это API без главной страницы; работайте через http://localhost:8000 |
| Grafana на `:3000` открывается без стилей | UI Grafana открывайте через портал: http://localhost:8000/grafana/ |
| порт занят | поменяйте его в `.env` (раздел 13) |
| Mac уснул во время долгого прогона | запускайте под `caffeinate -i` |
| другая машина, сценарий slow-cpu ведёт себя иначе | runner сам калибрует квоту CPU по baseline; проверьте всё через `make acceptance` |

Логи control plane (их нет в Loki, это намеренно):

```bash
docker compose logs fault-injector --tail 50
docker compose logs experiment-runner --tail 50
```

---

## 16. Шпаргалка команд

```bash
# запуск и проверка
make up                 # поднять всё
make open               # открыть Control Center http://localhost:8000
make smoke              # быстрая проверка
make ps / make logs     # статус / логи

# работа
make load RATE=5 DURATION=120
make fault TYPE=<тип> [TARGET=<цель>] [PARAMS='{"…":…}'] [EXP=<id>]
make faults             # активные фолты
make recover            # снять все фолты

# эксперименты
make scenarios
make experiment SCENARIO=<имя>
make experiments
make acceptance [FULL=1] [ROUNDS=2] [SCENARIOS="a b"]

# тесты
make test | make test-faults | make test-experiments | make test-challenges

# уборка
make down               # остановить (данные сохраняются)
make reset              # стереть всё и начать с нуля
```

**Один адрес для всего: http://localhost:8000** (`make open`) — Control Center: Overview, Faults,
Experiments, Logs & traces, Dashboards. Клиенты приложения ходят на `http://localhost:8080`.
