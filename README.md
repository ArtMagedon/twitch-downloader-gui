# Twitch Downloader GUI

Linux GUI на Python/Tkinter для `TwitchDownloaderCLI`. Программа не заменяет CLI, а строит для него понятный графический интерфейс: получение информации о VOD/clip, динамический список качеств, скачивание VOD/клипов/чата, обновление чата, рендер чата, очередь заданий, управление FFmpeg/cache/update и настройки.

## Что нужно

- Python 3.10+ с `tkinter`.
- `TwitchDownloaderCLI` из официального проекта.
- Для chat render на Ubuntu нужны `fontconfig` и `libfontconfig1`; FFmpeg можно использовать системный или указать отдельный путь.

На Ubuntu/Debian:

```bash
sudo apt install python3-tk fontconfig libfontconfig1 ffmpeg curl unzip
```

Потом:

```bash
./install_cli.sh
./install.sh
```

После установки приложение появится в меню приложений и доступно как:

```bash
~/.local/bin/twitch-downloader-gui
```

`install_cli.sh` получает последний Linux x64 CLI через GitHub Releases API и кладёт его в `bin/TwitchDownloaderCLI`. Можно вместо этого указать уже установленный CLI во вкладке «Настройки».

## Запуск без установки

```bash
./run.sh
```

## Реализованные функции

- VOD / highlight download: quality, trim, threads, bandwidth, trim mode, OAuth, FFmpeg, temporary path, collision behavior.
- Clip download: quality, bandwidth, metadata, FFmpeg, temporary path, collision behavior.
- Chat download: JSON / JSON.GZ / HTML / TXT, trim, embeds BTTV/FFZ/7TV, timestamp format, threads.
- Chat update: conversion, compression, embed missing, replace embeds, trims, BTTV/FFZ/7TV.
- Chat render: size, FPS, update rate, font, styles, colors, badges, timestamps, filters, emotes, avatars, scaling, FFmpeg args.
- Queue: sequential execution, progress parsing, stop current task, remove/clean finished jobs.
- Tools: help, FFmpeg download, cache clear, CLI update, TS merge.
- Persistent configuration in `~/.config/twitch-downloader-gui/config.json`.

## Архитектура

UI -> command builder -> `TwitchDownloaderCLI` via `subprocess.Popen` -> stdout/stderr -> queue/progress parser.

`info --format raw` используется для получения метаданных и динамического списка качеств. Для VOD CLI возвращает JSON-информацию, данные chapters и M3U8 playlist; для clip — JSON-информацию. Это соответствует текущей реализации `InfoHandler` CLI.

## Ограничение

В GUI намеренно нет собственной реализации Twitch API/загрузчика. Все сетевые и медиаоперации выполняет официальный `TwitchDownloaderCLI`, поэтому совместимость определяется его текущей версией.
