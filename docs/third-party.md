# Сторонние компоненты

- [Alisa Voice](https://github.com/rison1337/Alisa-ChatGPT-voice-assistant) — GPT bridge; скачивается при установке. Полная программа в этом Git не хранится.
- [lite-kit](https://github.com/krevetkqqq/lite-kit) и [HackAlice](https://hackalice.me/) — материалы по подготовке колонки; локальный wrapper использует сервер кита и его TLS-файлы из отдельного cache-каталога, не копируя их в Git.
- [Dropbear](https://github.com/mkj/dropbear) — SSH на подготовленной колонке; [static-bins](https://github.com/pierzchalski/static-bins) — источник socat. Dropbear для подготовки загружается из [yandex-station-binaries](https://github.com/Marker284/yandex-station-binaries).
- [Zig](https://ziglang.org/) — компилятор ARMhf-модуля; [linkedom](https://github.com/WebReflection/linkedom) — DOM-тесты.

Лицензии сторонних компонентов принадлежат их авторам; лицензия build-скриптов не заменяет лицензии бинарников. Общая лицензия чужому коду не назначается. Firmware fallback и TLS-файлы загружаются локально в игнорируемые каталоги, в Git не входят. Данные аккаунтов не распространяются.
