# Установка на Fedora

Выполняйте из корня репозитория обычным пользователем. Станция Мини 2 должна иметь root SSH, известный host key и отключённые автообновления. Если колонка ещё не подготовлена, сначала выполните [root, установку SSH и отключение OTA](rooting-mini2.md).

## Подготовка

```bash
sudo dnf install python3 git openssh-clients chromium xorg-x11-server-Xvfb xorg-x11-xauth pulseaudio-utils opus
cp config.example.toml config.local.toml
chmod 600 config.local.toml
```

Заполните host/port/identity и Bluetooth MAC. Не путайте Bluetooth и Wi-Fi MAC. При необходимости создайте отдельный SSH-ключ через `ssh-keygen -t ed25519` и добавьте public key через прежний доверенный доступ; старые ключи не заменяйте. Сверьте host fingerprint перед первым подключением.

Сопрягите станцию в настройках Fedora и вручную проверьте обычный звук по Bluetooth.

```bash
python3 scripts/alice.py doctor --ssh
```

Ожидается доступный Bluetooth sink, инструменты, поддерживаемая модель, прошивка и ELF32 ARM. Если SSH/аудио недоступны — сначала восстановите соединение. `chromium-browser` также поддерживается.

## Программа и колонка

```bash
python3 scripts/build-agent.py --fetch --work-dir "$HOME/.cache/alice-root/build-agent"
python3 scripts/alice.py install --component all --agent "$HOME/.cache/alice-root/build-agent/native_agent_music.so"
python3 scripts/alice.py deploy
```

Загружается текущий релиз upstream. Можно передать собственный архив через `--archive FILE`. Для GPT без музыкальных команд используйте `--component bridge`, для одного SoundCloud — `--component soundcloud`.

`install` сохраняет backup и устанавливает локальные файлы; `deploy` доставляет компоненты на подготовленную колонку. Ни одна команда не запускает сервис. Перед изменением работающего моста остановите его явно. План без изменений: `install --component all --dry-run` и `deploy --dry-run`.

## Вход и запуск

```bash
python3 scripts/alice.py login chatgpt
python3 scripts/alice.py login soundcloud
python3 scripts/alice.py service enable
python3 scripts/alice.py service start
python3 scripts/alice.py verify --interactive
```

Войдите в собственные аккаунты в отдельных браузерных профилях. Подписки остаются у аккаунтов. Сервис запускается после входа в Fedora; `enable` включает автозапуск, `start` запускает мост сейчас.

Проверьте обычную Алису, GPT, SoundCloud/лайки, паузу/продолжение, голосовые команды вне GPT и звук по Bluetooth. `verity-sc background` скрывает окно, `window` возвращает его с тем же профилем.

Если Bluetooth пропал — восстановите соединение, не перезапускайте весь аудиостек. При проблемах SoundCloud используйте `verity-sc diagnose`; `signed_in: null` не доказывает выход из аккаунта. Если новая структура upstream не распознана, потребуется поправить интеграцию в исходниках.
