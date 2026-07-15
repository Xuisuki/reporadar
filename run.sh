#!/usr/bin/env bash
# RepoRadar — запуск. Один вызов = один пост в канал.
#   ./run.sh            один пост
#   ./run.sh preview    сухой прогон без отправки
#   ./run.sh chatid     определить id канала
set -euo pipefail
cd "$(dirname "$0")"
VPY=".venv/bin/python"; [ -f "$VPY" ] || VPY=".venv/Scripts/python"
[ -f "$VPY" ] || { echo "Сначала запустите ./install.sh"; exit 1; }
exec "$VPY" bot.py "${@:-run}"
