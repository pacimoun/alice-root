# Root Мини 2: пошаговый мастер

Нужны Fedora, Станция Мини 2 и телефон с «Домом с Алисой». Если SSH и блокировка OTA уже работают, этот этап пропускается — [установка bridge и SoundCloud](install-fedora.md).

## Запуск

```bash
sudo dnf install android-tools python3 openssl curl openssh-clients iproute bind-utils
```

Создайте `config.local.toml` по [примеру](../config.example.toml), если его ещё нет. В существующий конфиг добавьте секцию `[rooting]`. Заполните:

- `station.host` — зарезервированный DHCP IPv4 колонки; `station.identity` — путь отдельного SSH-ключа.
- `rooting.pc_ip`, `rooting.router_ip` — LAN-адреса ПК и роутера.
- `rooting.wifi_mac` — **Wi-Fi**, не Bluetooth MAC колонки. Его можно посмотреть среди клиентов роутера до сброса.

```bash
python3 scripts/rooting.py --dry-run
python3 scripts/rooting.py
```

Мастер сам скачает файлы, проверит ПК, запустит сервер и проведёт через шаги. Отдельного запуска `root-server.py` и длинных блоков команд не требуется.

## Что происходит

1. **Файлы.** Прошивка `.46` скачивается по официальному URL Яндекса в `firmware/yandexmini_2/`. Готовая локальная копия используется как fallback. Kit, TLS-файлы и Dropbear кешируются в `.cache/rooting/`. Внешних root-серверов/зеркал нет. Файл `.zip` фактически содержит подписанный CPIO OTA — он передаётся колонке без изменений.
2. **Локальный сервер.** До подмены DNS сохраняются настоящие upstream IP. Мастер временно разрешает TCP 80/443 только от колонки; при выходе убирает только свои правила и процесс. Он не отключает firewalld, SELinux или VPN.
3. **Роутер — вручную.** Мастер создаёт готовый `router-rooting.sh` с вашими параметрами и показывает его путь. Скопируйте его на OpenWrt и выполните `sh router-rooting.sh apply`. Позже — `sh router-rooting.sh cleanup`. Скрипт меняет только именованные объекты своего стенда, не upstream DNS/Podkop/AdGuard. Hostrecord трёх имён действует на всех клиентов dnsmasq, перенаправление DNS — только на MAC колонки. На другом роутере вручную направьте `quasar.yandex.net`, `quasar.yandex.ru`, `scbh.yandex.net` на ПК; AAAA для них должен отвечать без IPv6-адреса.
4. **Сброс и Wi-Fi — вручную.** Mini 2: отключить питание → зажать отключение микрофонов → подключить питание → держать 5 секунд до жёлтого → отпустить. В приложении настройте Wi-Fi. Телефон лучше оставить на мобильном интернете и Bluetooth, отключив Wi-Fi. Дождитесь OTA/перезапуска, питание во время прошивки не отключайте. Мастер проверит ADB/root и версию; можно повторять проверку, пока колонка загружается.
5. **Закрепление доступа.** Мастер создаст SSH-ключ ПК, если его нет, установит постоянный SSH, добавит public key с сохранением старых и отключит OTA. Сохранит оригиналы/журнал и скопирует backup на ПК. Покажет fingerprint из ADB для сверки при первом SSH-входе. До успешной проверки SSH и OTA DNS возвращать нельзя.
6. **Завершение.** Мастер остановит сервер и удалит свои временные правила Fedora. На роутере вручную выполните `cleanup`; мастер проверит возврат DNS. Проверьте обычную Алису, Bluetooth и VPN. Перезагрузка колонки автоматически не выполняется.

## Отдельные шаги и повторный запуск

```bash
python3 scripts/rooting.py fetch           # только файлы; можно заранее и без колонки
python3 scripts/rooting.py router-script   # подготовить файл для роутера
python3 scripts/rooting.py check           # ПК, TLS и upstream DNS; без открытия портов
python3 scripts/rooting.py cleanup         # убрать оставшийся после сбоя стенд на ПК
```

После сбоя также выполните `sh router-rooting.sh cleanup` на роутере. Backups мастер не удаляет; их путь выводится в конце. Подготовку после ошибки восстанавливает `scripts/station-tools.py recover --adb SERIAL --backup /data/opt/alice-root-preparation/tx.ID`; отмена OTA-guard — отдельная `ota-restore`, обычный uninstall её не вызывает.

Все действия мастера доступны отдельными командами в `python3 scripts/rooting.py --help`. Ключ с паролем вводится интерактивно/через ssh-agent; приватные ключи и локальный конфиг не попадают в Git. Firmware fallback также не коммитится; в новом checkout он скачивается автоматически.

Источники: [официальный сброс Mini 2](https://alice.yandex.ru/support/ru/station/settings/reset-mini2.html), [root/DNS](https://hackalice.me/start/root/), [SSH/OTA](https://hackalice.me/start/ssh/), [lite-kit](https://github.com/krevetkqqq/lite-kit). Прошивка: `0.369.1.46.4778262943.20261013.299`; официальный URL встроен в `src/alice_root/firmware.py`.
