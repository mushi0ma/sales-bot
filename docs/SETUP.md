# sales-bot — тестовая среда (ИИ-агент продаж для ЖКХ)

Изолированная ВМ (QEMU/KVM + passt, без libvirt) на хосте CachyOS. Внутри — стек fazer.ai, собранный из исходников:
Chatwoot-форк (+ встроенный нативный WhatsApp-коннектор) и fazer.ai agents. LLM — OpenRouter, бесплатные модели.
**Только тестовые данные. Секретов в репозитории нет.**

## Состав репозитория

| Путь | Что |
|---|---|
| `infra/vm.conf` | параметры ВМ (4 vCPU, 6 ГБ RAM, 60 ГБ, ssh 127.0.0.1:2222) |
| `infra/vm.sh` | create / start / stop / status / ssh / wait / tunnel / snapshot / revert / snapshots / console |
| `infra/cloud-init/*.tmpl` | cloud-init: пользователь `dev` (только ключ), Docker, ufw, swap 4 ГБ, ротация логов |
| `infra/guest/egress-fw.sh` | файрвол исходящего трафика в госте (см. ниже) |
| `infra/sync.sh` | rsync `vendor/` + `stack/` → `~/work` в ВМ |
| `infra/vm/` | диски, seed, ssh-ключ ВМ, run-файлы — **в .gitignore** |
| `vendor/` | shallow-клоны fazer-ai/* — **в .gitignore** |
| `stack/docker-compose.yml` | стек, собирается из `vendor/` внутри ВМ |
| `stack/gen-env.sh` | генерирует `stack/.env` с секретами внутри ВМ (chmod 600) |
| `stack/agent/system-prompt.md` | системный промпт агента |
| `stack/tickets/server.py` | мок-диспетчерская (заявки) для HTTP-инструментов агента |
| `stack/bootstrap.py` | первичная настройка Chatwoot/agents, инструменты заявок, playground |
| `stack/tests/scenarios.py` + `scenarios.json` | сценарии через настоящий пайплайн agents (playground): инструменты, RAG, guardrail |

## Требования хоста

- CPU с VT-x/AMD-V, доступный `/dev/kvm`.
- `sudo pacman -Syu --needed qemu-base passt libisoburn` (полное обновление: частичное на Arch не поддерживается;
  при устаревших базах пакетов зеркала отдают 404).
- libvirt, systemd-юниты и правила файрвола хоста **не нужны**.

## Создание ВМ с нуля

```sh
# 1. образ Ubuntu 24.04 + проверка
cd infra/vm/images
curl -fLO https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img
curl -fLO https://cloud-images.ubuntu.com/releases/noble/release/SHA256SUMS
curl -fLO https://cloud-images.ubuntu.com/releases/noble/release/SHA256SUMS.gpg
gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81
gpg --verify SHA256SUMS.gpg SHA256SUMS && grep server-cloudimg-amd64.img$ SHA256SUMS | sha256sum -c -
cd -
# 2. диск (chattr +C на каталоге ДО создания qcow2 — btrfs) + cloud-init seed
mkdir -p infra/vm && chattr +C infra/vm   # один раз, на пустом каталоге
infra/vm.sh create
# 3. запуск и ожидание cloud-init (~5–10 мин)
infra/vm.sh start && infra/vm.sh wait
```

## Стек: сборка, запуск, первичная настройка

```sh
infra/sync.sh                                   # код хоста -> ВМ (~/work)
infra/vm.sh ssh 'cd work/stack && ./gen-env.sh' # один раз: секреты стека -> stack/.env (600) в ВМ
# ключ OpenRouter -> ВМ файлом (stdin, не argv, не печатается):
grep '^OPENROUTER_API_KEY=' .env | infra/vm.sh ssh 'umask 077; mkdir -p ~/secrets; cat > ~/secrets/openrouter.env'
infra/vm.sh ssh 'cd work/stack && docker compose build cw-rails && docker compose build agents'  # ~17 мин
infra/vm.sh ssh 'cd work/stack && docker compose up -d'
infra/vm.sh ssh 'cd work/stack && python3 -I bootstrap.py all'          # Chatwoot admin, agents /setup, vault, агент, связка
infra/vm.sh ssh 'cd work/stack && python3 -I bootstrap.py playground'   # smoke-test без WhatsApp
```

`bootstrap.py` пишет учётные данные только в ВМ: `~/secrets/chatwoot-admin.env`, `~/secrets/agents-admin.env` (600).
Посмотреть пароль — в своём терминале (не в чате с ИИ): `infra/vm.sh ssh cat secrets/chatwoot-admin.env`.

| Сервис | Адрес (после `infra/vm.sh tunnel`) | Health-check |
|---|---|---|
| Chatwoot (форк) | http://localhost:3000 | `curl -s localhost:3000/api` → 200 |
| agents (консоль, playground) | http://localhost:3100 | `curl -s localhost:3100/api/health` → `status: ok` |
| Postgres ×2, Redis | только внутри docker-сети ВМ | `docker compose ps` → healthy |

Ежедневно: `infra/vm.sh start` → `infra/vm.sh tunnel` (держать открытым) … `infra/vm.sh stop`.
Стек поднимается сам (`restart: unless-stopped`). Логи: `infra/vm.sh ssh 'cd work/stack && docker compose logs -f agents'`.

### Кому отвечает бот

Агент в `mode: production` — отвечает сам, без `/teste`, но **только тестировщикам**: контактам с галочкой
«Тестировщик бота» (атрибут контакта `salesbot_tester` в Chatwoot). Остальным — молча ничего (правило допуска
agents `contactAuth`, отказ без копии и без handoff): номер WhatsApp — настоящая SIM, Telegram-бота может найти кто
угодно, а данные в стеке только тестовые.
- Добавить тестировщика: Chatwoot → контакт → «Атрибуты контакта» → «Тестировщик бота» ✓, или
  `python3 -I bootstrap.py testers <contact_id>...` (только явные id: «всем сразу» впустило бы и постороннего).
- Вернуть режим «молчит до `/teste`»: `AGENT_MODE=test python3 -I bootstrap.py agent`.
- Открыть всем: `BOT_AUDIENCE=everyone` — bootstrap откажется, пока в `knowledge.md` тестовые заглушки. Любое
  другое значение (опечатка) — отказ; после записи bootstrap читает агента обратно и падает, если гейт не включён.
- Оговорка: правило по атрибуту контакта надёжно, пока к агенту не привязан web-widget inbox (там посетитель сам
  задаёт атрибуты через SDK).
- `/reset` в диалоге сбрасывает память бота.

### Оператор ↔ бот

- **Бот передал оператору** (`handoff_to_human`) или **оператор сам ответил** в диалоге (публичный ответ из Chatwoot
  agents считает перехватом) → диалог `open`, бот в нём молчит (лог: `reason=taken_over`). Приватные заметки
  перехватом не считаются.
- **Вернуть боту в любой момент:** в диалоге Chatwoot → «Макросы» → **🤖 Вернуть боту** (снимает назначение,
  ставит `pending`, оставляет заметку). Бот ответит на следующее сообщение жильца и **помнит переписку**, включая
  ответы оператора (agents дописывает их в память диалога). То же в консоли agents: Conversations → диалог →
  **Return to AI**. «Завершить» (resolve) тоже возвращает: следующее сообщение жильца откроет диалог как `pending`.
- Макрос и атрибут создаёт `python3 -I bootstrap.py operator` (входит в `all`).
Автоназначение в inbox должно быть **выключено** (`bootstrap.py bind` делает это сам): иначе Chatwoot сразу
назначает каждый новый диалог оператору, и бот молчит с первого сообщения (`taken_over`).

Ротация токена/секрета бота (если засветились): удалить Agent Bot в Chatwoot (Settings → Bots или
`DELETE /api/v1/accounts/1/agent_bots/<id>`), затем `POST /api/v1/chatwoot/inboxes/<id>/reconnect` в agents —
он создаст нового бота с новыми токеном, HMAC-секретом и route-токеном.

### Снапшоты

| Снапшот | Состояние |
|---|---|
| `base-clean` | чистая ВМ после cloud-init, без секретов и стека |
| `stack-ready` | стек собран и настроен, секреты внутри, **WhatsApp не сопряжён** |

`infra/vm.sh stop && infra/vm.sh revert <имя> && infra/vm.sh start`. После сопряжения — см. политику ниже.

## Сеть и изоляция

- `passt --no-map-gw --ipv4-only`: гость **не** видит loopback хоста; IPv6 выключен.
- Наружу проброшен только SSH: `127.0.0.1:2222` хоста → 22 гостя. Веб-интерфейсы — только через
  `infra/vm.sh tunnel` (ssh -L на 127.0.0.1 хоста): Chatwoot `http://localhost:3000`, agents `http://localhost:3100`.
- DNS гостя: 1.1.1.1, 9.9.9.9 (резолвер хоста недоступен намеренно).
- ufw в госте: входящие — только 22/tcp.
- Исходящие (`infra/guest/egress-fw.sh`, systemd-юнит `sales-bot-egress-fw`):
  - `OUTPUT` → цепочка `SALESBOT-OUT`: ESTABLISHED,RELATED, `lo`, `-o docker0`, `-o br-+`, DHCP → разрешено;
    затем DROP на 10/8, 172.16/12, 192.168/16, 100.64/10 (Tailscale), 169.254/16.
  - `DOCKER-USER` → цепочка `SALESBOT-FWD`: ESTABLISHED,RELATED, `-i br-+ -o br-+`, `-i docker0 -o docker0`
    → разрешено; затем те же DROP. Мосты разрешены по интерфейсу, а не по подсети: подсети 172.x совпадают
    с docker-сетями хоста.
  - Интернет (DNS, apt, docker pull, OpenRouter, WhatsApp) работает.

## WhatsApp-коннектор

- Библиотека: **whatsmeow** (`go.mau.fi/whatsmeow`), сервис `fazer-ai/whatsapp-connector` (Go, MIT). Не Baileys:
  Baileys/Z-API в форке помечены «замороженными» (legacy), `baileys-api` не используется.
- Режим: встроенный (embedded) в контейнер `cw-sidekiq`; бинарник берётся из готового образа
  `ghcr.io/fazer-ai/whatsapp-connector:0.9.0@sha256:995a580c61364141fae139e476f62d29f32f7d08a87aac3f236c85d0c90df1bc`
  при сборке Chatwoot (не из исходников).
- Подключение: Chatwoot → Settings → Inboxes → Add Inbox → WhatsApp → **WhatsApp (native)** → номер → QR
  (или код сопряжения, если WhatsApp требует passkey и QR не проходит). Затем привязать inbox к агенту:
  `python3 -I bootstrap.py inboxes` → `python3 -I bootstrap.py bind <id>`.
- Сессия (связанное устройство, ключи Signal/Noise) хранится в Postgres Chatwoot, в отдельной БД
  `chatwoot_production_whatsapp_connector` (создаётся автоматически; пользователю БД нужен CREATEDB).
  Владелец сессии определяется арендой в Redis; события/команды ходят через Redis Streams.
- Медиа коннектора — том `wac_media` (`WAC_MEDIA_ROOT=/app/wac-media`); исходящие вложения коннектор забирает
  у Rails по `INTERNAL_HOST_URL=http://cw-rails:3000`.
- Звонки (UDP) выключены.

### ⚠️ Сессия WhatsApp = секрет. Политика снапшотов

- Содержимое БД `*_whatsapp_connector` (и тома Postgres целиком) даёт полный доступ к номеру как связанное устройство.
  Не копировать из ВМ, не коммитить, не делиться дампами.
- Снапшот `base-clean` делается **до** установки секретов и **до** сканирования QR.
- **После сопряжения снапшоты ВМ не откатывать**: откат вернёт старое состояние ключей (ratchet), возможна
  рассинхронизация, потеря сообщений и разлогин. Вместо отката: отвязать устройство в телефоне
  (WhatsApp → Связанные устройства), в Chatwoot переподключить inbox и **заново отсканировать QR**.
- Использовать только вторую SIM: неофициальное подключение несёт риск блокировки номера.

## LLM (OpenRouter)

- Ключ хранится на хосте в `./.env` (`OPENROUTER_API_KEY`, в .gitignore). В ВМ передаётся файлом
  `~/secrets/openrouter.env` (chmod 600) и заносится в зашифрованное хранилище (vault) agents через REST изнутри ВМ.
  Значение ключа нигде не печатается.
- Бесплатные модели (`:free`) — $0 (проверено: `usage: 0`); на ключе лимит трат $5.
- Основная: `nvidia/nemotron-3-ultra-550b-a55b:free`. Запасные: `nvidia/nemotron-3.5-super-vl:free`,
  `google/gemma-4-31b-it:free` (часто 429).
- Жёсткое правило в промпте: суммы, тарифы, даты, юридические формулировки — только из ответов инструментов
  (включая `search_knowledge`), иначе «Уточню у оператора…» + `handoff_to_human`. Проверяет это не промпт, а
  guardrail agents «factual grounding» (см. «Справочник УК»).
- **При каждой смене модели, промпта или инструментов** в ВМ: `python3 -I tests/scenarios.py` (из `~/work/stack`).
  Сценарии идут через playground agents — та же модель, промпт, инструменты, база знаний и guardrail, что у живого
  бота; сценарий лишь говорит, какие инструменты должны (или не должны) быть вызваны. Playground не создаёт
  диалога, поэтому заявки из сценариев — без телефона. `say` может быть списком реплик (один диалог, `threadId`).
  Handoff в playground только симулируется, и модель пишет после него ещё строку; в бою же `customerMessage`
  handoff — последнее, что видит жилец, поэтому прогон проверяет именно его.

## Внешние обращения стека

- agents: `FAZER_AI_HUB_URL=""` — к app.fazer.ai не ходит.
- Chatwoot: `DISABLE_TELEMETRY=true`.
- Сборка Chatwoot скачивает официальный `enterprise/` с GitHub только для сверки хеша и сборки ассетов;
  в итоговый образ он не попадает, Enterprise не активируется (нет `CW_EDITION=ee`).
- Установщик fazer.ai (`install.sh` → `bunx @fazer-ai/agents@latest`) **не используется**: он требует OAuth-входа
  на app.fazer.ai и глобально ставит плагин Claude Code.

## Проверено end-to-end (2026-11-12)

Телефон → вторая SIM (WhatsApp native) → Chatwoot → agents (`nemotron-3-ultra:free`) → ответ в WhatsApp и в Chatwoot.
`/teste` включает диалог; заявка на домофон — бот собирает адрес; вопрос о цене — личная заметка с причиной +
`handoff_to_human` (диалог переоткрыт и назначен оператору). Импорт истории при сопряжении: 0 диалогов.

## Заявки (тестовая диспетчерская)

Kanban в открытом форке Chatwoot — только заглушка Pro, поэтому заявки ведёт отдельный мок-сервис `tickets`
(`stack/tickets/server.py`: stdlib Python + SQLite, том `tickets_data`). Порт наружу не публикуется, доступ только
из docker-сети по Bearer-токену `TICKETS_API_TOKEN` (`stack/.env`, в vault agents — запись «Tickets (test dispatcher)»,
kind `bearer_token`). В agents он разрешён через `SSRF_INTERNAL_TARGETS=…,tickets:8080` + `allowedHosts: ["tickets"]`.

| Инструмент агента | Что делает | Откуда данные |
|---|---|---|
| `create_ticket` | POST заявки, ответ: номер `ГГГГ-NNNNN`, статус, срок (`sla_text`) — только данные, без указаний боту | модель: категория, срочность, адрес, квартира, суть; сервер: `conversation_id`, телефон, имя из контекста |
| `get_my_tickets` | последние 5 заявок **этого** телефона | телефон только из контекста диалога, модель его не задаёт |
| `add_ticket_note` | дополнение к **своей** заявке (уточнение, что на фото, время доступа) | модель: номер, текст; сервер проверяет владельца по диалогу, телефону или контакту — чужая заявка → 404, закрытая → 409 |
| `submit_meter_reading` | приём показаний счётчика, номер приёма `П-ГГГГ-NNNNN`, предыдущее показание и расход | модель: тип, показание, зав. номер, адрес, квартира; сервер: контекст диалога |

- Повтор в том же диалоге (та же категория, адрес и квартира, 24 ч) возвращает открытую заявку (`duplicate: true`);
  если повтор аварийный, а открытая заявка обычная — она повышается до аварийной (эскалация).
- Заявки жильца ищутся по телефону **или** `contact_id` Chatwoot (у WhatsApp-контакта без телефона — по контакту).
- Показания: тот же счётчик = тип + адрес/квартира (+ зав. номер, если есть у обоих). Меньше предыдущего — HTTP 409
  (`expectedStatuses: [409]`, бот просит перепроверить); то же значение за 24 ч — `duplicate: true`. Цифры с фото
  бот сначала подтверждает у жильца, набранные текстом передаёт сразу. Список: `server.py readings`.
- Сроки (`SLA` в `server.py`) вымышленные; модель вправе их называть только потому, что они пришли из инструмента.
- Встроенные инструменты агента ограничены явным списком: `handoff_to_human`, `private_note`, `set_labels`,
  `get_current_time` (без явной строки NATIVE agents выдаёт все, включая `resolve_conversation` и `send_image`).
- Настройка: `./gen-env.sh` (допишет токен в существующий `.env`), `docker compose up -d tickets agents`,
  `python3 -I bootstrap.py agent && python3 -I bootstrap.py tickets`.
- «Добавил к заявке» бот говорит только при `added: true`. Посмотреть дополнения: `server.py notes 2026-00006`.
- Оператору: `docker compose exec tickets python3 -I /app/server.py list` и
  `… set-status 2026-00001 in_progress|done|cancelled`.
- В playground нет телефона и `conversation_id`: `get_my_tickets` честно отвечает «телефон неизвестен».

## Голосовые (локальный STT)

Голосовые распознаёт сервис `stt` в ВМ: `stack/stt/` (faster-whisper 1.2.1, модель `large-v3-turbo` int8,
скачивается **при сборке** образа и вшивается в него; в работе `HF_HUB_OFFLINE=1`). Голос жильцов не покидает ВМ,
денег не стоит. Отвечает в формате OpenAI `/v1/audio/transcriptions`, в agents подключён как провайдер
`openai-compatible` (vault «STT (local whisper)», `baseUrl http://stt:8000/v1`, Bearer `STT_API_TOKEN` из `stack/.env`).

- Скорость: ~0,75× реального времени на 4 vCPU (7 с аудио → ~5 с). Таймаут STT в agents — 60 с, значит голосовые
  длиннее ~75 с не распознаются (бот получит пометку «аудио не разобрано»). Модель `small` в 4 раза быстрее,
  но заметно хуже по-русски: `WHISPER_MODEL=small docker compose build stt` + `STT_MODEL_ID=whisper-small`.
- Память: ~1,8 ГБ (`mem_limit: 2560m`). Образ 3,9 ГБ.
- `av==18.1.0` закреплён: в av 19 убран аргумент `metadata_errors`, который передаёт faster-whisper 1.2.1.
- Вложения agents скачивает сам: `data_url` от `localhost:3000` переписывается на `cw-rails:3000`
  (`attachmentFetchTarget`, потому что `cw-rails:3000` объявлен в `SSRF_INTERNAL_TARGETS`).
- `/v1/vault/test` для этой записи всегда отвечает `blocked_url` (проба идёт через SSRF-guard) — это норма.
  Проверка целиком: `POST /api/v1/agents/1/playground/audio/transcribe` (multipart `file`).
- Настройка: `./gen-env.sh`, `docker compose build stt && docker compose up -d stt`, `python3 -I bootstrap.py stt`.

**Внутренняя сеть `internal` (`internal: true`, без выхода в интернет):** в ней `stt` и `tickets`; `agents`
подключён к обеим сетям. Причина: внутри faster-whisper есть onnxruntime с кодом телеметрии Microsoft
(в логе `telemetry_1ds`), а этим двум сервисам интернет не нужен. Проверено: из `stt` наружу — `URLError`,
из `agents` доступны `stt`, `tickets`, `cw-rails` и OpenRouter.

## Изображения (vision)

Фото и документы во входящих читает `nvidia/nemotron-3.5-super-vl:free` через OpenRouter (vault «OpenRouter»,
`bootstrap.py vision`), промпт извлечения русский и про ЖКХ (`VISION_PROMPT`: счётчики, повреждения, квитанции;
«не додумывай, неразборчивое отметь»). Картинки уходят в OpenRouter, как и текст диалога, — данные только тестовые.
~3–25 с на фото (очередь free-модели), $0.
`reasoningEffort: "none"` обязателен: модель рассуждающая и без него тратит 400–700 токенов на «мысли», а agents
даёт одной картинке 20 с (`VISION_IMAGE_CEILING_MS`) — в WhatsApp фото падало по таймауту. Проверка: `POST /api/v1/agents/1/playground/file/extract` (multipart `file`); у
`playground/file` нет подписи к файлу — адрес передавайте следующим сообщением.

## Справочник УК (база знаний)

`stack/agent/knowledge.md` — справочник: экстренные номера (112/101/103/104 — настоящие), вымышленные АДС, офис,
часы приёма, окно приёма показаний, цены 4 платных услуг. Это **база знаний agents** («Справочник УК (тест)»,
`bootstrap.py knowledge` пересоздаёт её из файла), а не текст в промпте: бот ищет в ней инструментом
`search_knowledge` (RAG), в промпте только правила. Править можно и в консоли agents → Knowledge, но следующий
`bootstrap.py knowledge` вернёт содержимое файла. При `BOT_AUDIENCE=everyone` bootstrap откажется загружать
справочник с тестовыми заглушками (`000-00-0`, `.example`, «Тестовая»).

**Проверка фактов — guardrail agents «factual grounding»** (`settings.guardrails.factualGrounding`, задаёт
`bootstrap.py agent`): перед отправкой отдельная модель-рецензент (`GROUNDING_MODEL`, по умолчанию
`nvidia/nemotron-3-ultra-550b-a55b:free`) сверяет ответ с тем, что вернули `search_knowledge`, `create_ticket`,
`get_my_tickets`, `add_ticket_note`, `submit_meter_reading`; цифра, телефон или обещание без источника → бот пишет
ответ заново (`action: revise`). Модерация входа/выхода выключена. Вердикт виден в trace playground и в логах agents
(`check: factual_grounding`). Пример находки: «оператор подключится через минуту» — такого срока нет в источниках.
- Рецензенту agents даёт 30 с (`REVIEW_TIMEOUT_MS`, не настраивается). На бесплатных моделях это предел: проверено
  2026-11-12 — `google/gemma-4-26b-a4b-it:free` 429 на каждый запрос, `nvidia/nemotron-3.5-lightning:free` то 429,
  то таймаут; лучше всех держится основная ultra (≈1 сбой на 13 ходов). Сбой проверки не блокирует ответ
  (`onFailure: pass`) — ответ уходит непроверенным; сценарный прогон помечает такой ход `UNCHECKED`. С платной
  моделью (грант) рецензента стоит сделать другого семейства, чем основную.
- Guardrail проверяет только ходы, где вызывался инструмент-источник. Обещание без инструментов («оператор
  подключится через несколько секунд» при простой просьбе позвать человека) он не видит — это остаётся на промпте.

**Эмбеддинги (RAG):**
- Колонка векторов в agents жёстко 1536, а бесплатные `nvidia/nemotron-3.5-embed-*:free` по умолчанию отдают
  2048/4096. Патч `stack/patches/agents-embedding-dimensions.patch` добавляет `dimensions: 1536` в запрос при
  `EMBEDDING_SEND_DIMENSIONS=true` (задано в compose). vendor/ не в git, поэтому после `git pull` в vendor/agents:
  `stack/patches/apply.sh`, затем `docker compose build agents`.
- `python3 -I bootstrap.py knowledge`: эмбеддинги `nvidia/nemotron-3.5-embed-8b:free`, база «Справочник УК (тест)»
  из `agent/knowledge.md` (пересоздаётся из файла), проверочный поиск.
- ⚠️ `PUT /v1/tenant-settings/embedding` сохраняет **только** `credentialRef` (provider/model/baseURL молча
  отбрасываются), а адрес берётся из записи vault, **только если её kind `openai_compatible`**. С записью kind
  `openrouter` agents отправляет ключ на `api.openai.com` (так и случилось 2026-11-12: 2 запроса, ответ 401).
  Поэтому для эмбеддингов отдельная запись «OpenRouter (embeddings)» kind `openai_compatible`, baseUrl
  `https://openrouter.ai/api/v1`; bootstrap проверяет kind и адрес и иначе отказывается. Модель задаётся на базе
  (`embeddingModel`), не в настройках.
Форматирование: `**жирный**` Chatwoot сам переводит в WhatsApp-разметку, а строка `---` уходит отдельным
сообщением — промпт её запрещает.

Грант Google: что запросить и как это ляжет на agents — [GOOGLE-GRANT-REQUEST.md](GOOGLE-GRANT-REQUEST.md).

## Telegram

Inbox Chatwoot «Telegram» на бота из @BotFather. Chatwoot для Telegram регистрирует вебхук на `FRONTEND_URL`, а
Telegram ходит только на публичный HTTPS — у ВМ его нет. Поэтому вебхук не ставится, а сервис `tg-bridge`
(`stack/tg-bridge/bridge.py`, stdlib) опрашивает `getUpdates` и передаёт каждое обновление в собственный маршрут
Chatwoot `POST http://cw-rails:3000/webhooks/telegram/<token>` внутри docker-сети. Ответы Chatwoot отправляет сам
через `api.telegram.org`. Мост наружу ходит только в `api.telegram.org` и токен не пишет в лог. Chatwoot писал бы
токен из пути вебхука в лог запросов и в аргументы задания — это закрывает патч
`stack/patches/chatwoot-telegram-token-logs.patch` (маска пути, `bot_token` в filter_parameters,
`log_arguments = false`); после `git pull` в vendor/chatwoot: `stack/patches/apply.sh` и пересборка cw-rails.
Обновление подтверждается Telegram только после ответа Chatwoot 2xx: перезапуск Chatwoot ничего не теряет, но
таймаут после уже принятого обновления даст одно повторное сообщение.

1. @BotFather → `/newbot` → токен. Записать на хосте в `./.env`: `TELEGRAM_BOT_TOKEN=…` (**не в чат**).
2. `infra/push-secret.sh TELEGRAM_BOT_TOKEN` — переносит одну строку в `~/work/stack/.env` ВМ через stdin ssh.
3. В ВМ: `docker compose --profile telegram up -d tg-bridge`, затем `python3 -I bootstrap.py telegram` (inbox
   без вебхука, автоназначение выключено, привязка к агенту).
4. Написать боту в Telegram; в Chatwoot у нового контакта поставить «Тестировщик бота» (или
   `bootstrap.py testers <id>`), написать ещё раз — бот ответит.

- Сохранение inbox в настройках Chatwoot снова ставит вебхук (и падает на http-адресе). Мост сам снимает вебхук,
  если Telegram ответит 409, но настройки Telegram-inbox в UI лучше не трогать.
- У Telegram-контакта нет телефона: `get_my_tickets` ищет заявки по `contact_id`.

## Админ-панели

| Где | Адрес (через `infra/vm.sh tunnel`) | Для чего |
|---|---|---|
| Chatwoot | http://localhost:3000 (`admin@salesbot.test`, пароль в ВМ `~/secrets/chatwoot-admin.env`) | рабочее место оператора: диалоги, макрос «🤖 Вернуть боту», контакты и галочка «Тестировщик бота», inbox'ы (WhatsApp, Telegram), личные заметки бота |
| agents | http://localhost:3100 (пароль в ВМ `~/secrets/agents-admin.env`, интерфейс на английском) | настройка бота: промпт, модель, инструменты, голос/фото, правило допуска; Conversations (+ **Return to AI**), Knowledge, Logs (почему бот молчал: `taken_over`, `contact_auth`…), Playground |

- Диалог, который ведёт бот, имеет статус **«Ожидающие» (pending)**, а список диалогов Chatwoot по умолчанию
  открыт на «Открытые» — без фильтра «Все» кажется, что диалоги пропали. `bootstrap.py operator` ставит
  администратору фильтр статуса «Все»; новым операторам — переключить статус в шапке списка.
- Telegram-контакты (без телефона и e-mail) не видны в общем списке «Контакты» — искать через диалог.

Всё, что настраивает `bootstrap.py`, при повторном запуске перезапишет ручные правки в консоли agents
(промпт, режим, допуск) — постоянные изменения вносить в `stack/agent/*.md` и `bootstrap.py`.

## Открытые задачи
- [ ] При запахе газа handoff происходит только после адреса (номера 104/112 бот теперь берёт из справочника).
- [ ] Перепроверить фото из WhatsApp после `reasoningEffort: none` (до этого — таймаут). Голосовое — проверено.
- [ ] Окно приёма показаний 20–25 инструмент не проверяет (бот может «принять» 3-го числа).
- [ ] Upstream (fazer-ai/chatwoot): маска токена Telegram в логах (наш патч).
- [ ] Upstream (fazer-ai/agents): issue/PR на `dimensions` и на молчаливый fallback эмбеддингов к api.openai.com.
- [ ] Dev-режим Chatwoot с горячей перезагрузкой для правок админ-панели (сейчас — полная пересборка образа).
