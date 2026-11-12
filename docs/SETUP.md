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
| `stack/tests/model_guard.py` | регрессионный тест модели (цены/тарифы/handoff) |

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

Агент создаётся в `mode: test`: **молчит в любом диалоге, пока в нём не написать `/teste`** (защита от ответов
реальным людям, пишущим на тестовый номер). `/reset` сбрасывает память диалога.

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
- Жёсткое правило в промпте: суммы, тарифы, даты, юридические формулировки — только из инструментов/базы знаний,
  иначе «Уточню у оператора…» + `handoff_to_human`.
- **При каждой смене модели** прогонять:
  `python3 -I stack/tests/model_guard.py --key-file <файл с ключом> <model>` (кейс «Сколько стоит домофон?» и др.).

## Внешние обращения стека

- agents: `FAZER_AI_HUB_URL=""` — к app.fazer.ai не ходит.
- Chatwoot: `DISABLE_TELEMETRY=true`.
- Сборка Chatwoot скачивает официальный `enterprise/` с GitHub только для сверки хеша и сборки ассетов;
  в итоговый образ он не попадает, Enterprise не активируется (нет `CW_EDITION=ee`).
- Установщик fazer.ai (`install.sh` → `bunx @fazer-ai/agents@latest`) **не используется**: он требует OAuth-входа
  на app.fazer.ai и глобально ставит плагин Claude Code.

## Открытые задачи

- [ ] **Медиа (голосовые/изображения) во входящих**: URL вложений строится от `FRONTEND_URL=http://localhost:3000`,
      который внутри контейнера agents указывает в никуда → STT/vision не получат файл. Варианты: общий
      hostname для браузера и контейнеров (`*.localhost` + алиас в docker-сети) или прокси. Сначала текстовый сценарий.
- [ ] База знаний (RAG): бесплатные эмбеддинги у OpenRouter есть (`nvidia/nemotron-3.5-embed-8b:free` и др.), не настроено.
- [ ] Dev-режим Chatwoot с горячей перезагрузкой для правок админ-панели (сейчас — полная пересборка образа).
