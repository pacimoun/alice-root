# Backup, откат и удаление

Команды выполняются из checkout с тем же локальным конфигом. Установщик хранит резервные копии и журнал принадлежащих ему файлов в private state-каталоге.

```bash
python3 scripts/alice.py backup
```

Для отката сначала остановите компоненты, затем отмените доставку и локальную установку:

```bash
python3 scripts/alice.py service stop
python3 scripts/alice.py service disable
verity-sc stop
python3 scripts/alice.py rollback-station
python3 scripts/alice.py rollback
```

Если устанавливался только SoundCloud, шаги с bridge и колонкой пропускаются. `rollback` отменяет последнюю локальную установку; `uninstall` — все локальные установки проекта. Аккаунты, профили, backups, системные пакеты, root SSH и OTA-guard сохраняются.

После аварийного завершения используйте `recover /ABSOLUTE/BACKUP/DIRECTORY`; при потере SSH восстановите доступ и повторите `rollback-station`. Сохраняйте backups до завершения отката. Если конфиг изменялся после установки, сначала сохраните его: откат не удаляет изменённые настройки автоматически.

Локальные кэши сборки, `build/`, `*.egg-info` и `node_modules/` можно удалить и получить заново. `config.local.toml`, браузерные профили и backup-каталоги — пользовательские данные, их автоматически не чистим.

Подготовка колонки: `station-tools.py recover --adb SERIAL --backup /data/opt/alice-root-preparation/tx.ID` восстанавливает файлы прерванной подготовки. `ota-restore` отдельно возвращает исходные OTA-файлы; SSH при этом сохраняется. Детали — в [инструкции root](rooting-mini2.md).
