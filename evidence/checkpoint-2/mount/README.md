# Проверка файловой службы Mac и SSHFS

[Машиночитаемый результат](sshfs-coordinated.json) содержит 28 из 28 успешных проверок на синтетической папке. Linux наблюдал настоящее монтирование `fuse.sshfs`. В операции записи проверялись двоичные данные, повторная запись по смещениям, усечение, дописывание, новый каталог, переименование черновика, согласованное переименование двух существующих файлов и согласованное удаление. При конфликте пользовательский файл сохранил значение `9000`. Проверка обращалась непосредственно к доверенному помощнику Linux. Вызовы шлюза и языковой модели, запуск приложения с окном и замеры производительности в число этих проверок не входят.

[Режим чтения](sshfs-read-only.json) прошёл 7 из 7 проверок. Ядро Linux подтвердило `ro`, запись отклонена. В [опыте с обрывом](sshfs-disconnect.json) прошли 8 из 8 проверок. При одном принятом байте в черновике результат оказался неизвестным, оригинал сохранил `8000`, старое поколение повторно не подключилось.

Для воспроизведения нужны Mac со Swift и Python, выделенный профиль Colima и SSHFS версии 3.7.6. У профиля выключено подключение общих папок Mac. Контекст Docker пользователя сохраняется. Из корня репозитория, подставив имя выделенного профиля:

```sh
colima ssh --profile itmo-checkpoint2 -- sh < prototypes/mounting/prepare_linux.sh
python3 scripts/prepare_colima_workspace.py --profile itmo-checkpoint2 --output .local/checkpoint2
python3 -m venv /private/tmp/itmo-mount-venv
/private/tmp/itmo-mount-venv/bin/python -m pip install -r requirements-mount.txt
source .local/checkpoint2/mount.env
/private/tmp/itmo-mount-venv/bin/python -m scripts.mount_smoke /private/tmp/itmo-sshfs-result.json --coordinated
/private/tmp/itmo-mount-venv/bin/python -m scripts.mount_smoke /private/tmp/itmo-sshfs-read-only.json --case read-only
/private/tmp/itmo-mount-venv/bin/python -m scripts.mount_smoke /private/tmp/itmo-sshfs-disconnect.json --case disconnect
```

Сценарий создаёт собственную папку с двумя контрольными файлами и освобождает монтирование в конце. Параметр `--coordinated` относится к синтетическому сценарию с контролируемыми локальными писателями. [Политика подключения и публикации](../../../docs/reference/mounting.md)
