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
| `stack/caddy/Caddyfile` | HTTPS (Let's Encrypt) только для Chatwoot |
| `stack/group-watcher/` | потребности в WhatsApp-группах → предложение в личку (путь A), каталог услуг, датасет |
| `stack/agent/` | промпты: бот поддержки (`system-prompt.md`, `knowledge.md`) и продавец (`seller-prompt.md`) |
| `stack/voice/` | голосовой агент: звонки в WhatsApp и обзвон от имени председателя ОСИ (`dial.sh`, `campaigns.json`) |
| `stack/tickets/`, `stack/stt/`, `stack/tg-userbot/` | тестовая диспетчерская, распознавание голосовых, Telegram-userbot |
| `stack/patches/` | наши патчи к vendor: эмбеддинги agents, звонки WhatsApp голосовому агенту, исходящий звонок со `stream` |
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
| Chatwoot (оператор) | `https://sales-bot.eknot.kz` (`PUBLIC_DOMAIN`); без него туннель `deploy/ssh.sh -- -N -L 3000:127.0.0.1:3000` и http://localhost:3000 | `curl -s localhost:3000/api` → 200 |
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
- **Размышления Flash урезаны** (`reasoning_effort: low` в `config.yaml`). По умолчанию модель тратила ~850
  токенов на размышление и 5–7 с на вызов, а ответ продавца это 2–3 вызова. С `low` вызов 2,5–3 с, ход продавца
  5–10 с вместо 20–30; сценарии продавца 15/15.
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

- Коннектор **whatsmeow** (`fazer-ai/whatsapp-connector`), встроен в `cw-sidekiq`. Бинарник собираем сами из
  `vendor/whatsapp-connector` с нашим патчем (образ `salesbot/whatsapp-connector:0.9.0-stream`, раздел «Голос»). Подключение: Chatwoot → Settings → Inboxes → Add Inbox → WhatsApp → **WhatsApp (native)** → номер → QR.
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
- Пачка собирается `BATCH_SECONDS` (20 с): от сообщения в группе до предложения в личке ~25–45 с.
- **Ответы в самой группе** (`faq.py`, `FAQ_MODE=auto`). Вопрос в чат про e-KNOT (цена, тариф, как работает
  услуга, Аудитор ЖКХ) получает ответ сразу, реплаем на вопрос, по каталогу и снимкам eknot.me. Вопрос про
  конкретный дом (сумма взноса, долги, решения ОСИ) остаётся без ответа: этих данных у бота нет. Ответ, верный
  месяц и дольше, кешируется с эмбеддингом вопроса: тот же вопрос другими словами получает ответ из кеша за ~0,5 с
  без вызова модели. Кеш привязан к хешу материалов: обновили снимки сайта, старые ответы перестают действовать.
  Ответ получает каждый спросивший, а не только первый: в большом чате один вопрос задают многие. Пропускается
  только повтор того же вопроса тем же человеком в течение 12 ч. Новых ответов модели не больше 6 в час на группу
  (расход), всех ответов не больше 40 в час (защита от флуда). На сообщение, на которое ответили в группе,
  предложение в личку не отправляется.
- **Ответы, которые дала сама группа.** Участник ответил реплаем WhatsApp на вопрос («на какой счёт скидываться?»
  → «Kaspi Gold …»): пара запоминается для этой группы на 30 дней. Следующий, кто спросит то же своими словами,
  получает «Здесь уже отвечали (Айгуль): «…»», реплаем на свой вопрос. Те, кто спросил то же за последние 3 часа
  и остался без ответа, получают его сразу после ответа участника. Как свой факт бот это не подаёт, в другие
  группы не переносит. Новый ответ на тот же вопрос заменяет старый (сменились реквизиты). Не запоминаются
  ответы на свой же вопрос, встречные вопросы, «+1», «тоже», «не знаю». «стоп» от автора стирает и его ответы.
- **Тема лички.** Память agents общая на контакт: старые диалоги человека попадают в контекст. Поэтому
  group-watcher пишет в атрибут диалога `salesbot_offer`, какое предложение открыло диалог и что человек писал в
  группе, а продавец видит этот атрибут в промпте (`settings.attributeContext`).
- Каталог услуг: `stack/group-watcher/services.json` (7 услуг eknot.me и Аудитор ЖКХ). Из него же `bootstrap.py
  seller` собирает базу знаний продавца: после правки каталога `--force-recreate group-watcher` и `seller`.

Команды (на ВМ, `docker compose exec group-watcher python3 -I /app/watcher.py …`):

| Команда | Что |
|---|---|
| `stats` | счётчики за всё время |
| `report [дней]` | цифры для ежедневного отчёта: воронка от сообщения до лида, доставка, ответы за 24 ч, отказы |
| `eval /app/samples.json` | точность и полнота распознавания на размеченных сообщениях |
| `forget <contact_id>` | для тестов: снять отказ, предложения и паузу 7 дней у контакта |
| `faq` | закешированные ответы: вопрос, ответ, сколько раз использован, источник (`kb` или группа и автор) |
| `faq-ask "вопрос"` | что ответит бот в группе (кладёт в кеш, в чат ничего не пишет) |
| `faq-off <id>` | выключить закешированный ответ |

## Продавец e-KNOT

Агент «Продавец e-KNOT (тест)» (`bootstrap.py seller`) ведёт личку после предложения. Пишет как «помощник e-KNOT»,
коротко, ответ дробится на сообщения до 280 знаков с «печатает…» (`settings.split`, 200 слов в минуту, пауза
0,8–3,5 с). Факты только из базы знаний «Услуги e-KNOT (eknot.me)». Цены называет только публичные, с оговоркой,
что точный расчёт делает менеджер. На цену и подключение ставит метку `lead` и
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

- **Канал Telegram: userbot** (решение ментора): бот читает группы и пишет в личку от обычного аккаунта, как в
  WhatsApp. Bot API не используется: 26.11 бота @oknewsales_bot подключили по ошибке и в тот же день убрали
  (вебхук снят, входящие удалены; 27.11 убраны и остатки: секрет `telegram-bot-token`, шаг `bootstrap.py telegram`,
  проверка вебхука в Caddy, два патча Chatwoot. Секрет `webhook-secret` ментора остался в Secret Manager, стек его
  не читает).
- **Userbot** (`stack/tg-userbot/`, профиль compose `tg-userbot`): спайк S2 на Telethon, заморожен до 06.12.
  Telethon архивирован, замена на Kurigram в бэклоге.

## Проверки

```sh
python3 -I stack/group-watcher/test_watcher.py                       # ноутбук, логика без сети
deploy/ssh.sh 'cd /srv/salesbot/stack && python3 -I tests/scenarios.py'            # бот поддержки, 14 сценариев
deploy/ssh.sh 'cd /srv/salesbot/stack && python3 -I tests/scenarios.py --seller'   # продавец, 15 сценариев
deploy/ssh.sh 'cd /srv/salesbot/stack && docker compose exec -T group-watcher python3 -I /app/watcher.py eval /app/samples.json'
```

Сценарии идут через playground agents: та же модель, промпт, инструменты, база знаний и проверка фактов, что у
живого бота. Playground не создаёт диалога Chatwoot, поэтому заявки из сценариев без телефона, а handoff только
симулируется: прогон проверяет `customerMessage` handoff, как увидит жилец. Ответы продавца дополнительно
проверяются на шаблонные ИИ-фразы, markdown и «я бот».

## Голос: звонки помощнику e-KNOT

Сервис `voice` (`stack/voice`): Pipecat 1.15 + Gemini Live (`gemini-3.8-live`, Vertex AI, регион `eu`, голос Kore,
ru-RU). Тот же промпт по смыслу, что у продавца, но для речи: короткие фразы, числа словами, ссылки не диктует.
Инструменты: `service_info` (страница услуги со снимка eknot.me), `save_lead` (заявка менеджеру), `end_call`.
Звонки и стенограммы в `voice.db` (том `voice_data`); логи без текста разговора. После каждого звонка в WhatsApp
агент кладёт приватную заметку в диалог этого человека в Chatwoot (диалог и контакт создаются, если их нет): входящий
или исходящий и повод, длительность, итог обзвона или заявка, стенограмма. Метки `call` и, если есть заявка, `lead`.
Пишет от API-пользователя group-watcher (`GW_CW_TOKEN`); без токена заметки не будет, звонок останется в `voice.db`.

Два входа в один и тот же разговор:

- **Звонок в WhatsApp на тестовый номер.** Коннектор WhatsApp внутри Chatwoot принимает голосовой звонок 1:1 и
  передаёт его голосом агенту по WebSocket `ws://voice:8090/wa` (16 кГц PCM, протокол `call.accept` со `stream` из
  `vendor/whatsapp-connector/contract/PROTOCOL.md`). В Chatwoot этого не было: патч
  `stack/patches/chatwoot-whatsapp-voice-agent.patch` при заданном `WHATSAPP_VOICE_AGENT_URL` подключает сессию с
  `calls.answer` и отдаёт каждый звонок агенту. Голос к серверам WhatsApp идёт по UDP 40000 (исходящий). A-запись
  для этого не нужна. Выключить: `VOICE_AGENT_URL=` в `stack/.env`, пересоздать `cw-sidekiq`.
- **Исходящий звонок от имени председателя ОСИ** (обзвон: сбор голосов, напоминание о взносах). Коннектор звонит
  с тестового номера и отдаёт голос агенту, как при входящем. В коннекторе v0.9.0 исходящий звонок умел только
  браузерный WebRTC: патч `stack/patches/whatsapp-connector-outbound-stream.patch` добавляет `call.start` со
  `stream` (тесты в патче), образ коннектора собирается у нас (`docker compose --profile build build wa-connector`,
  затем `build cw-rails`). Поводы и факты для разговора: `stack/voice/campaigns.json` (`votes`, `fees`, тестовые
  данные), промпт `stack/voice/prompt-outbound.md`, итог (`voted`, `will_pay`, `callback`, `refused`…) в таблице
  `outcomes`. Звонить только тем, кто согласился на тест; с 09:00 до 21:00 по Астане:
  `deploy/ssh.sh 'cd /srv/salesbot/stack && voice/dial.sh +77011234567 votes Имя'`.
- **Браузер** (отладка): на ноутбуке `deploy/ssh.sh -- -N -L 8090:127.0.0.1:8090`, затем http://localhost:8090.
  localhost считается защищённым адресом, поэтому микрофон работает без HTTPS. Нужны наушники.

Лимиты (против зацикливания и расхода): 2 звонка одновременно, 10 минут на звонок, 40 звонков в день
(`VOICE_MAX_CALLS`, `VOICE_MAX_CALL_SECS`, `VOICE_DAILY_CALLS`). Казахского в Gemini Live нет: на казахскую речь
агент по-казахски предлагает продолжить по-русски или написать в WhatsApp.

Замер (симуляция звонка коннектора, 26.11): приветствие через 1,2 с после ответа на звонок, ответ на вопрос о цене
через 1,2–2,0 с после конца фразы, включая поход в `service_info`.

```bash
deploy/ssh.sh 'cd /srv/salesbot/stack && docker compose exec -T voice python3 -c "
import sqlite3; c=sqlite3.connect(\"/data/voice.db\")
print(c.execute(\"SELECT id, door, round(ended_at-started_at), transcript FROM calls ORDER BY id DESC LIMIT 3\").fetchall())"'
```

## Путь B: официальный WhatsApp Cloud API

Путь A пишет в личку с того же номера WhatsApp, что читает группу. Путь B пишет с номера Cloud API: первое
сообщение только одобренным шаблоном Meta (категория Marketing, в Казахстане около $0.06 за доставку).
group-watcher уже умеет оба пути, нужен только inbox.

1. Нужна A-запись: Meta шлёт вебхуки на `https://PUBLIC_DOMAIN/webhooks/whatsapp/<номер>`.
2. В Meta for Developers: приложение типа Business, продукт WhatsApp, тестовый номер (или свой), постоянный
   токен системного пользователя. Токен и App Secret кладём в Secret Manager, не в чат и не в файлы.
3. Chatwoot: «Входящие» → WhatsApp → WhatsApp Cloud, ручная настройка (WABA id, phone number id, токен, App Secret).
   Chatwoot сам подпишет вебхук.
4. В WhatsApp Manager подать два шаблона, категория Marketing, параметры `{{1}}` имя, `{{2}}` услуга, `{{3}}` ссылка.
   Тексты должны совпадать с `TEMPLATE_TEXT` в `stack/group-watcher/watcher.py`:
   - `eknot_offer_ru` (ru): «Здравствуйте, {{1}}! Это помощник e-KNOT. Вы писали в чате дома о проблеме, и у нас
     есть подходящий сервис: {{2}}. Подробнее: {{3}}. Если интересно, ответьте на это сообщение. Чтобы больше не
     получать сообщения, ответьте «стоп».»
   - `eknot_offer_kk` (kk): «Сәлеметсіз бе, {{1}}! Бұл e-KNOT көмекшісі. Сіз үй чатында мәселе туралы жаздыңыз,
     бізде лайықты қызмет бар: {{2}}. Толығырақ: {{3}}. Қызықты болса, осы хабарламаға жауап беріңіз. Хабарламалар
     алмау үшін «стоп» деп жазыңыз.»
   Цитату из группы в шаблон не ставим: Meta не пропускает произвольный текст жильца в шаблон рекламы.
5. После одобрения: Chatwoot подтягивает шаблоны сам (`sync_templates`). Привязать продавца:
   `bootstrap.py bind-cw <id нового inbox> seller`.
6. В `stack/.env`: `GW_CLOUD_INBOX_ID=<id>`, `GW_SEND_PATH=SPLIT` (нечётные id контактов по B, чётные по A,
   один человек всегда на одном пути) или `B`. Затем `docker compose up -d group-watcher`.
7. `watcher.py report 7` при двух путях печатает воронку отдельно по A и по B.

Ограничение: участник группы, которого WhatsApp показывает только по LID (без номера), по пути B недоступен.
Такая потребность получает статус `failed:no_contact_inbox_B` и на путь A не перекладывается, чтобы деление A/B
оставалось случайным.

## Бэкап

Ночью (03:15 UTC, таймер `salesbot-backup`) `deploy/vm/backup.sh` выгружает базы Chatwoot (`chatwoot_production`) и agents прямо в бакет,
без записи на диск. БД WhatsApp-коннектора не выгружается (это секрет, см. выше). Восстановление из бэкапа ещё не
проверялось.

## Внешние обращения стека

- agents: `FAZER_AI_HUB_URL=""`, к app.fazer.ai не ходит. Chatwoot: `DISABLE_TELEMETRY=true`.
- Наружу ходят: LiteLLM в Vertex AI, коннектор в WhatsApp (и UDP 40000 для голоса), tg-userbot в Telegram (MTProto,
  когда профиль запущен), Caddy в Let's Encrypt.
- Установщик fazer.ai (`install.sh`) не используется: он требует OAuth-входа на app.fazer.ai.

## Открытые задачи

- [x] A-запись `sales-bot.eknot.kz` (зона eknot.kz у ps.kz) → сертификат, публичная ссылка.
- [ ] Путь B (WhatsApp Cloud API): приложение Meta, тестовый номер, шаблоны ru/kk, inbox `whatsapp_cloud`.
- [x] Голос: итоги обзвона, заявки и стенограммы в Chatwoot (приватная заметка, метки `call`, `lead`).
- [ ] Голос: два звонка одновременно на одном аккаунте WhatsApp не проверены; список обзвона из файла, повторный
  звонок по `callback`.
- [ ] Telegram-userbot: вход отдельным тестовым аккаунтом, затем `bootstrap.py tg-userbot`.
- [ ] Проверить восстановление из бэкапа на пустой копии.
- [ ] Окно приёма показаний 20–25 инструмент не проверяет.
- [ ] Upstream: `dimensions` эмбеддингов (fazer-ai/agents), `call.start` со `stream` (fazer-ai/whatsapp-connector).
