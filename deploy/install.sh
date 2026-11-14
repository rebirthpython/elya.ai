#!/usr/bin/env bash
# Установка бота на сервер (Linux с systemd): виртуальное окружение, сервис бота
# и таймер, который регулярно запускает deploy/update.sh.
#
# Запускать из клона репозитория от имени пользователя, под которым будет работать бот:
#   deploy/install.sh
# Повторный запуск безопасен: обновит зависимости и unit-файлы и перезапустит бота.
#
# Настройки через переменные окружения:
#   PYTHON           интерпретатор для создания .venv, по умолчанию python3 (нужен 3.12+)
#   RUN_TESTS        1: гонять тесты перед каждым обновлением (по умолчанию), 0: не гонять
#   UPDATE_SCHEDULE  как часто проверять обновления, в формате OnCalendar из systemd:
#                    minutely (по умолчанию), *:0/5 — раз в 5 минут, hourly — раз в час

set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
PYTHON="${PYTHON:-python3}"
RUN_TESTS="${RUN_TESTS:-1}"
UPDATE_SCHEDULE="${UPDATE_SCHEDULE:-minutely}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BOT_UNIT=elya-bot.service
UPDATE_UNIT=elya-update.service
TIMER_UNIT=elya-update.timer
USER_NAME="$(id -un)"

# Без SSH-сессии (например, после su) systemctl --user не находит менеджер пользователя.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

step() { printf '\n==> %s\n' "$*"; }
die() {
    printf '\nОШИБКА: %s\n' "$*" >&2
    exit 1
}

trap 'die "команда на строке $LINENO завершилась с ошибкой (подробности выше)"' ERR

if [[ $(uname -s) != Linux ]] || ! command -v systemctl >/dev/null; then
    die "нужен Linux с systemd (Ubuntu, Debian и т. п.)"
fi
command -v git >/dev/null || die "не установлен git"
if [[ $(id -u) == 0 ]]; then
    echo "ВНИМАНИЕ: бот будет работать от root. Безопаснее завести отдельного пользователя" \
        "(sudo adduser elya), склонировать репозиторий под ним и запустить установку от него." >&2
fi

cd "$REPO_DIR"
BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
[[ -n $BRANCH ]] || die "клон не стоит на ветке (detached HEAD). Переключись: git switch <ветка>"

step "Виртуальное окружение $VENV_DIR"
if [[ ! -x $VENV_DIR/bin/python ]]; then
    command -v "$PYTHON" >/dev/null ||
        die "не найден $PYTHON. Установи Python 3.12+ или укажи его: PYTHON=python3.12 deploy/install.sh"
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))' ||
        die "нужен Python 3.12+, а $PYTHON — это $("$PYTHON" -V 2>&1). Укажи другой: PYTHON=python3.12 deploy/install.sh"
    "$PYTHON" -m venv "$VENV_DIR" ||
        die "не удалось создать .venv (в Debian/Ubuntu поставь пакет: sudo apt install python3-venv)"
fi
REQUIREMENTS=requirements.txt
if [[ $RUN_TESTS == 1 ]]; then REQUIREMENTS=requirements-dev.txt; fi
"$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check -r "$REQUIREMENTS"

step "Проверка .env"
if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    die "создан .env из шаблона. Заполни в нём BOT_TOKEN, ALLOWED_USER_IDS, GEMINI_API_KEY" \
        "и запусти установку ещё раз"
fi
"$VENV_DIR/bin/python" - <<'PY' || die "исправь .env и запусти установку ещё раз"
import sys
from config.settings import ConfigError, load_settings

try:
    load_settings()
except ConfigError as exc:
    sys.exit(f"Ошибка в .env: {exc}")
PY

step "Автозапуск после перезагрузки сервера (loginctl enable-linger)"
if [[ "$(loginctl show-user "$USER_NAME" --property=Linger --value 2>/dev/null || true)" != yes ]]; then
    loginctl enable-linger "$USER_NAME" 2>/dev/null ||
        die "не хватает прав. Выполни от администратора: sudo loginctl enable-linger $USER_NAME —" \
            "и запусти установку ещё раз"
fi
for _ in $(seq 20); do
    systemctl --user show-environment >/dev/null 2>&1 && break
    sleep 0.5
done
systemctl --user show-environment >/dev/null 2>&1 ||
    die "не удаётся подключиться к systemd пользователя (systemctl --user)." \
        "Зайди на сервер по SSH сразу под пользователем $USER_NAME (не через su/sudo) и повтори"

step "Сервисы systemd в $UNIT_DIR"
mkdir -p "$UNIT_DIR"
# В unit-файлах % — спецсимвол, его нужно удваивать.
WD="${REPO_DIR//%/%%}"

cat >"$UNIT_DIR/$BOT_UNIT" <<EOF
[Unit]
Description=Elya AI: Telegram-бот
# Перезапускать без ограничения числа попыток (например, пока на сервере нет сети).
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$WD
ExecStart="$WD/.venv/bin/python" main.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

cat >"$UNIT_DIR/$UPDATE_UNIT" <<EOF
[Unit]
Description=Elya AI: обновление бота из git

[Service]
Type=oneshot
WorkingDirectory=$WD
ExecStart=/bin/bash "$WD/deploy/update.sh"
Environment=RUN_TESTS=$RUN_TESTS
EOF

cat >"$UNIT_DIR/$TIMER_UNIT" <<EOF
[Unit]
Description=Elya AI: проверка обновлений в git

[Timer]
OnCalendar=$UPDATE_SCHEDULE
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --quiet "$BOT_UNIT" "$TIMER_UNIT"
systemctl --user restart "$BOT_UNIT" "$TIMER_UNIT"

step "Проверка запуска бота"
sleep 5
if ! systemctl --user is-active --quiet "$BOT_UNIT"; then
    journalctl --user --unit="$BOT_UNIT" --lines=30 --no-pager 2>&1 || true
    die "бот не запустился, причина в логе выше"
fi

cat <<EOF

Готово. Бот запущен и будет обновляться сам, когда в ветке $BRANCH
на GitHub появятся новые коммиты (проверка: $UPDATE_SCHEDULE).

Полезные команды:
  journalctl --user -u ${BOT_UNIT%.service} -f       лог бота
  journalctl --user -u ${UPDATE_UNIT%.service} -f    лог обновлений
  systemctl --user restart ${BOT_UNIT%.service}      перезапустить бота (например, после правки .env)
  deploy/update.sh --force               переустановить последнюю версию вручную
  systemctl --user disable --now $TIMER_UNIT   отключить автообновление
EOF
