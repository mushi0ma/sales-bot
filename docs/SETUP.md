# sales-bot: развёртывание и эксплуатация

Стек работает на ВМ Google Cloud компании e-KNOT (проект, зона, имя ВМ, домен и бакет в `deploy/gcp.env`).
Внутри Docker Compose: Chatwoot-форк fazer.ai со встроенным WhatsApp-коннектором, fazer.ai agents, LiteLLM
(шлюз к Gemini на Vertex AI), group-watcher, тестовая диспетчерская, локальный STT, Caddy.
**Только тестовые данные. Секреты только в Secret Manager, в репозитории их нет.**

## Состав репозитория

| Путь | Что |
|---|---|
| `deploy/gcp.env` | куда разворачивать: проект, зона, ВМ, домен, бакет, `VM_ROOT=/srv/salesbot` |
| `deploy/ssh.sh` | shell или одна команда на ВМ через IAP (публичного SSH нет); `-- -N -L …` для туннелей |
| `deploy/sync.sh` | `stack/` и `deploy/` с ноутбука на ВМ. Источник правды: этот checkout. `.env` и ключи не отправляет |
| `deploy/secrets.sh`, `deploy/secrets.map` | секреты в Secret Manager: `status`, `init` (создать и заполнить случайными), `import` |
| `deploy/vendor.lock` | исходники fazer.ai, закреплённые по коммиту |
| `deploy/vm/provision.sh` | разовая подготовка ВМ: Docker из apt, ротация логов, swap, каталог стека, таймер бэкапа |
| `deploy/vm/fetch-vendor.sh` | клонирует `vendor/` по `vendor.lock` и накладывает `stack/patches` |
| `deploy/vm/backup.sh` | ночной `pg_dump` обеих баз прямо в `gs://<бакет>/backups/` (хранение 30 дней) |
| `stack/docker-compose.yml` | стек |
| `stack/render-env.sh` | на ВМ собирает `stack/.env` (600) из Secret Manager через сервисный аккаунт ВМ, печатает только имена |
| `stack/bootstrap.py` | настройка Chatwoot и agents по шагам (`python3 -I bootstrap.py` без аргументов печатает список) |
| `stack/litellm/config.yaml` | модели Gemini и жёсткие лимиты rpm/rpd на каждую |
| `stack/caddy/Caddyfile` | HTTPS (Let's Encrypt) только для Chatwoot, проверка `secret_token` вебхука Telegram |
| `stack/group-watcher/` | потребности в WhatsApp-группах → предложение в личку (путь A), каталог услуг, датасет |
| `stack/agent/` | промпты: бот поддержки (`system-prompt.md`, `knowledge.md`) и продавец (`seller-prompt.md`) |
| `stack/tickets/`, `stack/stt/`, `stack/tg-userbot/` | тестовая диспетчерская, распознавание голосовых, Telegram-userbot |
| `stack/patches/` | наши патчи к vendor (эмбеддинги, логи и вебхук Telegram) |
| `stack/tests/` | сценарии через настоящий конвейер agents: бот поддержки и `--seller` |

## Что нужно на ноутбуке

- `gcloud` с входом в учётную запись, у которой есть доступ к проекту (IAP-туннель и OS Login на ВМ; для
  `deploy/secrets.sh` ещё `secretmanager.admin`).
- `rsync`, `ssh`, `python3`. Больше ничего: сборка и запуск идут на ВМ.

## Первое развёртывание

```sh
deploy/secrets.sh init                 # создать недостающие секреты, gen:* заполнить случайными значениями
deploy/secrets.sh status               # какие секреты заполнены (только имена)
deploy/sync.sh                         # stack/ и deploy/ -> ВМ
deploy/ssh.sh 'sudo bash /srv/salesbot/deploy/vm/provision.sh "$USER"'   # один раз
deploy/ssh.sh 'bash /srv/salesbot/deploy/vm/fetch-vendor.sh'             # vendor/ по vendor.lock + патчи
deploy/ssh.sh 'cd /srv/salesbot/stack && ./render-env.sh && docker compose build && docker compose up -d'
deploy/ssh.sh 'cd /srv/salesbot/stack && python3 -I bootstrap.py all'    # Chatwoot, agents, vault, бот поддержки, база, макрос
deploy/ssh.sh 'cd /srv/salesbot/stack && python3 -I bootstrap.py group-watcher && python3 -I bootstrap.py seller'
deploy/ssh.sh 'cd /srv/salesbot/stack && docker compose up -d --force-recreate group-watcher'  # прочитать GW_*
```

Секреты с пометкой `manual` в `deploy/secrets.map` (токены Meta) вносятся руками через stdin:
`printf %s '…' | gcloud secrets versions add <имя> --data-file=- --project=<проект>`. Значения никогда не
передаются аргументом и не печатаются. `deploy/secrets.sh` не меняет уже заданные значения: новый
`agents-encryption-key` сделал бы нечитаемым vault agents, новый пароль БД закрыл бы доступ к существующему тому.

`bootstrap.py` пишет учётные данные только на ВМ: `~/secrets/chatwoot-admin.env`, `~/secrets/agents-admin.env`
(600). Посмотреть пароль: в своём терминале, не в чате с ИИ.

## Обновление

```sh
deploy/sync.sh
deploy/ssh.sh 'cd /srv/salesbot/stack && docker compose up -d'           # пересоздаёт изменившиеся сервисы
```

- Файлы, смонтированные в контейнер (`watcher.py`, `services.json`, `tickets/server.py`, `litellm/config.yaml`),
  подхватываются только после `docker compose up -d --force-recreate <сервис>`.
- Промпты, база знаний и настройки агентов живут в agents: после правки `stack/agent/*` или `services.json`
  запустить нужный шаг (`agent`, `knowledge`, `seller`). Повторный запуск перезапишет ручные правки в консоли agents.
- Новая версия vendor: поменять коммит в `deploy/vendor.lock`, `fetch-vendor.sh`, `docker compose build <сервис>`,
  затем оба набора сценариев (ниже).
- Новый секрет: `deploy/secrets.sh`, затем на ВМ `./render-env.sh` и `docker compose up -d`. `render-env.sh`
  сохраняет ключи, которые записал `bootstrap.py` (`CW_TGU_*`, `GW_*`).

### База знаний продавца

Продавец ищет в трёх источниках: каталог `stack/group-watcher/services.json`, ответы на возражения
`stack/agent/seller-objections.md` и снимки страниц услуг eknot.me в `stack/kb/pages/`. Раз в неделю:
`python3 -I stack/kb/refresh.py` (показывает, что изменилось на сайте, код 1 при изменениях), затем прочитать
разницу (цены, обещания), `refresh.py --write`, коммит и `bootstrap.py seller`. Trafilatura проверяли: на этих
страницах она теряет таблицу тарифов онлайн-собраний, поэтому извлечение своё, без зависимостей.

## Доступ

| Что | Как | Health-check |
|---|---|---|
| Chatwoot (оператор) | `https://<PUBLIC_DOMAIN>` после A-записи; до неё туннель `deploy/ssh.sh -- -N -L 3000:127.0.0.1:3000` и http://localhost:3000 | `curl -s localhost:3000/api` → 200 |
| agents (консоль, playground) | только туннель: `deploy/ssh.sh -- -N -L 3100:127.0.0.1:3100`, http://localhost:3100 | `curl -s localhost:3100/api/health` |
| LiteLLM | только туннель: `deploy/ssh.sh -- -N -L 4000:127.0.0.1:4000`, ключ `LITELLM_MASTER_KEY` | `/health/liveliness` |
| Postgres ×2, Redis, tickets, stt, group-watcher | только внутри docker-сети ВМ | `docker compose ps` → healthy |

Наружу открыты только 80 и 443 (Caddy). Сертификат Caddy получит, когда `PUBLIC_DOMAIN` начнёт указывать на
внешний IP ВМ. Логи: `deploy/ssh.sh 'cd /srv/salesbot/stack && docker compose logs -f agents'`. Стек
поднимается сам после перезагрузки (`restart: unless-stopped`).

## Модели (LiteLLM → Vertex AI)

Все вызовы Gemini идут через LiteLLM: agents и group-watcher знают только `http://litellm:4000/v1` и
`LITELLM_MASTER_KEY`. LiteLLM ходит в Vertex AI от сервисного аккаунта ВМ, ключей API нет.

| Модель | Кто вызывает | Лимит (config.yaml) |
|---|---|---|
| `gemini-3.8-flash` | ответы бота поддержки и продавца, чтение фото | 40 rpm, 1000 rpd |
| `gemini-3.5-flash-lite` | поиск потребностей (group-watcher), проверка фактов, запасная модель агентов | 50 rpm, 3000 rpd |
| `gemini-4-argon` | только офлайн-задачи (разбор сложных случаев), в боевом конвейере не вызывается | 5 rpm, 30 rpd |
| `gemini-embedding-001` | база знаний (RAG), 1536 измерений | 300 rpm, 5000 rpd |

- **Запасная модель.** Gemini Flash иногда тратит токены на размышление и не возвращает ни текста, ни вызова
  инструмента (`finishReason: STOP` без частей). LiteLLM отдаёт это как `choices: []`, agents повторяет запрос на той
  же модели и получает то же. Поэтому у обоих агентов `settings.modelFallback` = Flash-Lite (`bootstrap.py`,
  `FALLBACK_MODEL`). Туда же уходят 429 и таймаут основной модели.
- **Проверка фактов** (guardrail agents «factual grounding»): перед отправкой Flash-Lite сверяет ответ с тем, что
  вернули инструменты-источники (`search_knowledge`, инструменты заявок). Цифра, телефон или обещание без
  источника → ответ переписывается (`action: revise`). Сбой рецензента не блокирует ответ (`onFailure: pass`),
  сценарии помечают такой ход `UNCHECKED`.
- **Эмбеддинги.** Колонка векторов в agents 1536, поэтому патч `agents-embedding-dimensions.patch` передаёт
  `dimensions`. Патч `agents-embedding-internal-targets.patch` разрешает эмбеддингам адрес `litellm:4000` внутри
  docker-сети (без него индексация падает с `ssrfBlocked`). `PUT /v1/tenant-settings/embedding` сохраняет только
  `credentialRef`, адрес берётся из записи vault (kind `openai_compatible`).
- После смены модели, промпта или инструментов: оба набора сценариев (раздел «Проверки»).

## Кому отвечают агенты

Оба агента в `mode: production` отвечают сами, но **только тестировщикам** и **только в личке**: правило допуска
agents `contactAuth` = атрибут контакта `salesbot_tester` («Тестировщик бота») **и** тип диалога `individual`.
Остальным молча ничего (без копии и без handoff). В WhatsApp-группе агенты не пишут никогда: группы читает
group-watcher.

- Добавить тестировщика: Chatwoot → контакт → «Атрибуты контакта» → «Тестировщик бота» ✓, или
  `python3 -I bootstrap.py testers <contact_id>...` (только явные id).
- Режим «молчит до `/teste`»: `AGENT_MODE=test python3 -I bootstrap.py agent` (то же для `seller`).
- Открыть всем: `BOT_AUDIENCE=everyone`. bootstrap откажется, пока в `knowledge.md` тестовые заглушки, и после
  записи читает агента обратно: если гейт не включён, падает.
- `/reset` в диалоге сбрасывает память бота.

Привязка inbox: один inbox = один агент. WhatsApp-номер (Chatwoot inbox 2) привязан к продавцу:
`python3 -I bootstrap.py bind-cw 2 seller`. Бот поддержки остаётся для Telegram. Автоназначение в inbox должно
быть выключено (`bind-cw` делает это сам), иначе Chatwoot назначает диалог оператору и бот молчит (`taken_over`).

## Оператор и бот

- **Бот передал диалог** (`handoff_to_human`) или **оператор ответил публично** → диалог `open`, бот в нём молчит.
  Приватные заметки перехватом не считаются.
- **Вернуть боту:** в диалоге Chatwoot → «Макросы» → **🤖 Вернуть боту** (снимает назначение, ставит `pending`).
  Бот помнит переписку, включая ответы оператора. То же в консоли agents: Conversations → **Return to AI**.
- Диалог, который ведёт бот, имеет статус «Ожидающие» (pending). Список диалогов Chatwoot по умолчанию открыт
  на «Открытые»: `bootstrap.py operator` ставит администратору фильтр «Все», новым операторам переключить вручную.
- Ротация токена бота: удалить Agent Bot в Chatwoot, затем `POST /api/v1/chatwoot/inboxes/<id>/reconnect` в agents.

## WhatsApp

- Коннектор **whatsmeow** (`fazer-ai/whatsapp-connector`), встроен в `cw-sidekiq`, бинарник из закреплённого
  образа. Подключение: Chatwoot → Settings → Inboxes → Add Inbox → WhatsApp → **WhatsApp (native)** → номер → QR.
  Затем `python3 -I bootstrap.py bind-cw <inbox_id> [seller]`.
- **Группы:** `WHATSAPP_GROUPS_ENABLED=true` в compose. Коннектор запоминает флаг групп при подключении сессии,
  поэтому после включения сессию нужно переподключить (в rails console:
  `Whatsapp::Session::Facade.new(channel).reassert_desired_state`). Группа появляется в Chatwoot отдельным
  диалогом с первым сообщением после этого.
- Неподписанные вебхуки Cloud API отклоняются: `WHATSAPP_ACCEPT_UNSIGNED_WEBHOOKS=false`.
- **Сессия WhatsApp = секрет.** БД `chatwoot_production_whatsapp_connector` даёт полный доступ к номеру как
  связанное устройство. Её не выгружают ни в бэкап, ни куда-либо ещё. После потери: в телефоне отвязать устройство,
  в Chatwoot переподключить inbox, заново QR (около минуты).
- Только тестовая SIM: неофициальное подключение несёт риск блокировки номера.

## group-watcher: потребности в группах (путь A)

```
сообщение в группе ─ вебхук Chatwoot (HMAC) ─ group-watcher ─ фильтр по корням ru/kk ─ Flash-Lite (пачка до 20 + 6 контекста)
   └─ потребность ─ карточка 💡 приватной заметкой в группе ─ правила ─ личное сообщение с услугой ─ «🤖 Вернуть боту» ─ продавец
```

- Читает только группы с меткой `salesbot-watch` (ставит оператор в Chatwoot). Сообщения до метки не учитываются.
- Правила отправки (по порядку): отказ «стоп» → уверенность ≥ 0,7 → одно предложение человеку в 7 дней → лимит в
  день (`GW_DAILY_SEND_CAP`, 20) → режим. Тихие часы 21–09 (Астана): предложение ждёт утра до 14 ч.
- Режимы `GW_SEND_MODE`: `auto` отправляет сам; `approve` ждёт оператора (`/offer N` или `/skip N` приватной
  заметкой); `dry` только карточки.
- «стоп» (стоп, stop, тоқта, отписаться…) в группе или в личке: отметка `salesbot_optout` у контакта, его
  сообщения удаляются из базы сервиса, ответ «Хорошо, больше не побеспокою».
- Каталог услуг: `stack/group-watcher/services.json` (7 услуг eknot.me и Аудитор ЖКХ). Из него же `bootstrap.py
  seller` собирает базу знаний продавца: после правки каталога `--force-recreate group-watcher` и `seller`.

Команды (на ВМ, `docker compose exec group-watcher python3 -I /app/watcher.py …`):

| Команда | Что |
|---|---|
| `stats` | счётчики за всё время |
| `report [дней]` | цифры для ежедневного отчёта: воронка от сообщения до лида, доставка, ответы за 24 ч, отказы |
| `eval /app/samples.json` | точность и полнота распознавания на размеченных сообщениях |
| `forget <contact_id>` | для тестов: снять отказ, предложения и паузу 7 дней у контакта |

## Продавец e-KNOT

Агент «Продавец e-KNOT (тест)» (`bootstrap.py seller`) ведёт личку после предложения. Пишет как «помощник e-KNOT»,
коротко, ответ дробится на сообщения до 280 знаков с «печатает…» (`settings.split`, 90 слов в минуту). Факты
только из базы знаний «Услуги e-KNOT (eknot.me)». Цены не называет. На цену и подключение ставит метку `lead` и
передаёт диалог менеджеру e-KNOT. Жилищные споры направляет в бесплатный Аудитор ЖКХ (zhkh.zerde.site). На
прямой вопрос «вы бот?» отвечает «цифровой помощник e-KNOT». Настройки голосовых и фото копирует у бота поддержки.

## Бот поддержки: заявки, голосовые, фото, справочник

- **Заявки** ведёт мок-сервис `tickets` (`stack/tickets/server.py`), инструменты агента `create_ticket`,
  `get_my_tickets`, `add_ticket_note`, `submit_meter_reading`. Сроки (`SLA`) вымышленные, модель называет их только
  потому, что они пришли из инструмента; аварийная заявка возвращает и телефон аварийно-диспетчерской службы.
  Оператору: `docker compose exec tickets python3 -I /app/server.py list | set-status <номер> <статус>`.
- **Голосовые** распознаёт `stt` на ВМ (faster-whisper `large-v3-turbo` int8, модель вшита в образ, работа
  офлайн). Голос жильцов не покидает ВМ. Таймаут STT в agents 60 с: голосовые длиннее ~75 с не распознаются.
- **Фото и документы** читает Gemini Flash (`bootstrap.py vision`) с русским промптом про ЖКХ.
- **Справочник УК** (`stack/agent/knowledge.md`) загружается в базу знаний agents (`bootstrap.py knowledge`),
  бот ищет в ней через `search_knowledge`. Номера 112/101/103/104 настоящие, остальное тестовое.
- Встроенные инструменты агентов ограничены явным списком (`handoff_to_human`, `private_note`, `set_labels`,
  `get_current_time`).
- `stt` и `tickets` в сети `internal` без выхода в интернет.

## Telegram

- **Бот** (`bootstrap.py telegram`): Chatwoot ставит вебхук на `https://<PUBLIC_DOMAIN>/webhooks/telegram/<token>` с
  `secret_token`; Caddy пропускает только вызовы с этим заголовком. Патчи Chatwoot маскируют токен в логах.
  **Нужна A-запись домена**: без публичного HTTPS Telegram вебхук не примет.
- **Userbot** (`stack/tg-userbot/`, профиль compose `tg-userbot`): спайк S2 на Telethon, заморожен до 06.12.
  Telethon архивирован, замена на Kurigram в бэклоге.

## Проверки

```sh
python3 -I stack/group-watcher/test_watcher.py                       # ноутбук, логика без сети
deploy/ssh.sh 'cd /srv/salesbot/stack && python3 -I tests/scenarios.py'            # бот поддержки, 14 сценариев
deploy/ssh.sh 'cd /srv/salesbot/stack && python3 -I tests/scenarios.py --seller'   # продавец, 12 сценариев
deploy/ssh.sh 'cd /srv/salesbot/stack && docker compose exec -T group-watcher python3 -I /app/watcher.py eval /app/samples.json'
```

Сценарии идут через playground agents: та же модель, промпт, инструменты, база знаний и проверка фактов, что у
живого бота. Playground не создаёт диалога Chatwoot, поэтому заявки из сценариев без телефона, а handoff только
симулируется: прогон проверяет `customerMessage` handoff, как увидит жилец. Ответы продавца дополнительно
проверяются на шаблонные ИИ-фразы, markdown и «я бот».

## Бэкап

Ночью (03:15 UTC, таймер `salesbot-backup`) `deploy/vm/backup.sh` выгружает базы Chatwoot (`chatwoot_production`) и agents прямо в бакет,
без записи на диск. БД WhatsApp-коннектора не выгружается (это секрет, см. выше). Восстановление из бэкапа ещё не
проверялось.

## Внешние обращения стека

- agents: `FAZER_AI_HUB_URL=""`, к app.fazer.ai не ходит. Chatwoot: `DISABLE_TELEMETRY=true`.
- Наружу ходят: LiteLLM в Vertex AI, коннектор в WhatsApp, Chatwoot в api.telegram.org, Caddy в Let's Encrypt.
- Установщик fazer.ai (`install.sh`) не используется: он требует OAuth-входа на app.fazer.ai.

## Открытые задачи

- [ ] A-запись `PUBLIC_DOMAIN` → сертификат, Telegram-бот, публичная ссылка для ментора.
- [ ] Путь B (WhatsApp Cloud API): приложение Meta, тестовый номер, шаблоны ru/kk, inbox `whatsapp_cloud`.
- [ ] Проверить восстановление из бэкапа на пустой копии.
- [ ] Окно приёма показаний 20–25 инструмент не проверяет.
- [ ] Upstream: маска токена Telegram в логах (fazer-ai/chatwoot), `dimensions` эмбеддингов (fazer-ai/agents).
