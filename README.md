# Алиса: GPT и SoundCloud на Fedora

Станция Мини 2 работает как Bluetooth-колонка для ПК, передаёт микрофон в ChatGPT Voice и принимает команды SoundCloud: «Алиса, музыка, мои лайки», «пауза», «продолжи», «следующий», «предыдущий», «включи …».

Нужны **Fedora x86_64**, подготовленная **Станция Мини 2 / YNDX-00021** с root SSH и отключённым автообновлением, Bluetooth и собственные аккаунты ChatGPT/SoundCloud. Прошивка: `0.369.1.46.4778262943.20261013.299`. GPT и SoundCloud требуют включённого ПК и входа пользователя. API-ключ не используется.

## Установка

**Новая колонка: сначала [root, постоянный SSH и отключение автообновлений](docs/rooting-mini2.md).** Заполните `[station]` / `[rooting]` в локальном конфиге и запустите `python3 scripts/rooting.py` — мастер сам скачает прошивку и проведёт через подготовку; роутер и сброс выполняются вручную.

**Колонка уже подготовлена:** [инструкция](docs/install-fedora.md): зависимости → локальный конфиг → Bluetooth → сборка → установка → вход в аккаунты → запуск сервиса.

```bash
cp config.example.toml config.local.toml
chmod 600 config.local.toml
# Заполните адрес, SSH-ключ и Bluetooth MAC своей колонки.
python3 scripts/alice.py doctor --ssh
```

Если всё уже работает, достаточно `doctor` и `status`. Установка не нужна для проверки состояния.

## Использование

```bash
python3 scripts/alice.py status
python3 scripts/alice.py service start
python3 scripts/alice.py service stop
verity-sc window          # окно браузера для входа
verity-sc background      # музыка без окна
verity-sc likes
verity-sc pause
verity-sc resume
verity-sc stop
```

GPT: «Алиса, открой чат» / «Алиса, закрой чат». Музыкальные команды работают вне GPT-чата. Обычный звук ПК и SoundCloud идут по Bluetooth; GPT использует отдельный аудиомост. Не запускайте второй bridge вручную рядом с сервисом.

[Backup, откат и удаление](docs/rollback.md). [Сторонние компоненты](docs/third-party.md).

## Разработка

`src/alice_root` — программа; `station` — нативный модуль/SSH; `scripts` — сборка и инструменты; `tests` — проверки. Используется текущий релиз Alisa Voice, доработки Fedora/Music применяются кодом интеграции. Запущенные программы сами не обновляются.

```bash
python3 -m unittest discover -s tests -v
npm ci && npm test
python3 scripts/check-repository.py
```

Для тестов интеграции: `python3 scripts/fetch-integration-fixtures.py --destination .cache/fixtures` и `python3 scripts/build-agent.py --fetch --host-tests --work-dir "$PWD/.cache/build-agent"`. Запускайте unittest с `ALICE_INTEGRATION_FIXTURES="$PWD/.cache/fixtures" ALICE_AGENT_TEST_DIR="$PWD/.cache/build-agent"`. Node/linkedom и Zig нужны только для разработки/сборки.
