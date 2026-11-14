#!/usr/bin/env bash
# Автообновление бота из git-репозитория.
#
# Если в отслеживаемой ветке на GitHub появились новые коммиты, скрипт:
#   1. подтягивает их (только fast-forward; .env и data/ не затрагиваются);
#   2. переустанавливает зависимости, если изменились requirements*.txt;
#   3. прогоняет тесты (отключается через RUN_TESTS=0);
#   4. перезапускает бота и проверяет, что он не упал за HEALTH_WAIT секунд;
#   5. при любой ошибке возвращает предыдущую версию и запоминает неудачный
#      коммит, чтобы не ставить его снова до следующего пуша.
#
# Обычно запускается раз в минуту таймером systemd (его ставит deploy/install.sh).
# Вручную:
#   deploy/update.sh            обновить, если есть новые коммиты
#   deploy/update.sh --force    переустановить последнюю версию, даже если она уже
#                               стоит или раньше не прошла проверку
#
# Настройки через переменные окружения:
#   DEPLOY_REMOTE  git-remote, по умолчанию origin
#   DEPLOY_BRANCH  ветка, по умолчанию та, на которой сейчас стоит клон на сервере
#   BOT_SERVICE    user-сервис systemd с ботом, по умолчанию elya-bot.service
#   RUN_TESTS      1: запускать pytest перед перезапуском (по умолчанию), 0: не запускать
#   HEALTH_WAIT    сколько секунд бот должен проработать без падений, по умолчанию 20
#   VENV_DIR       виртуальное окружение, по умолчанию .venv в корне репозитория

set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$REPO_DIR/.deploy"
FAILED_FILE="$STATE_DIR/failed_rev"

REMOTE="${DEPLOY_REMOTE:-origin}"
BOT_SERVICE="${BOT_SERVICE:-elya-bot.service}"
RUN_TESTS="${RUN_TESTS:-1}"
HEALTH_WAIT="${HEALTH_WAIT:-20}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
PY="$VENV_DIR/bin/python"

FORCE=0
BOT_RESTARTED=0

# Без SSH-сессии (cron, su) systemctl --user не находит менеджер пользователя.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

log() { printf '[update] %s\n' "$*"; }
die() {
    printf '[update] ОШИБКА: %s\n' "$*" >&2
    exit 1
}
svc() { systemctl --user "$@"; }
short() { git rev-parse --short "$1"; }
usage() { sed -n '2,/^$/{s/^# \{0,1\}//;p;}' "${BASH_SOURCE[0]}"; }

trap 'die "команда на строке $LINENO завершилась с ошибкой (подробности выше)"' ERR

requirements_file() {
    if [[ $RUN_TESTS == 1 ]]; then
        echo "requirements-dev.txt"
    else
        echo "requirements.txt"
    fi
}

install_deps() {
    local req
    req="$(requirements_file)"
    log "Устанавливаю зависимости из $req"
    "$PY" -m pip install --quiet --disable-pip-version-check -r "$REPO_DIR/$req"
}

# Нужно ли ставить зависимости при переходе с коммита $1 на $2.
deps_outdated() {
    if [[ $FORCE == 1 ]]; then return 0; fi
    if ! git diff --quiet "$1" "$2" -- 'requirements*.txt'; then return 0; fi
    if [[ $RUN_TESTS == 1 ]] && ! "$PY" -c 'import pytest' >/dev/null 2>&1; then return 0; fi
    return 1
}

run_tests() {
    if [[ $RUN_TESTS != 1 ]]; then return 0; fi
    log "Запускаю тесты"
    "$PY" -m pytest -q -p no:cacheprovider
}

main_pid() { svc show --property=MainPID --value "$BOT_SERVICE"; }

# Перезапускает бота и ждёт HEALTH_WAIT секунд. Бот считается рабочим, если он всё это
# время активен и это тот же процесс: при падении systemd перезапустит его с новым PID.
restart_bot() {
    log "Перезапускаю $BOT_SERVICE и проверяю его ${HEALTH_WAIT} с"
    BOT_RESTARTED=1
    svc restart "$BOT_SERVICE" || return 1
    local pid state
    pid="$(main_pid || true)"
    sleep "$HEALTH_WAIT"
    state="$(svc is-active "$BOT_SERVICE" || true)"
    if [[ $state == active && -n $pid && $pid != 0 && "$(main_pid || true)" == "$pid" ]]; then
        return 0
    fi
    log "Бот не работает стабильно (состояние: ${state:-неизвестно}). Последние строки его лога:"
    journalctl --user --unit="$BOT_SERVICE" --lines=20 --no-pager 2>&1 || true
    return 1
}

deploy() {
    local old=$1 new=$2
    if deps_outdated "$old" "$new"; then
        install_deps || return 1
    fi
    run_tests || return 1
    restart_bot
}

rollback() {
    local old=$1 new=$2
    log "Возвращаю версию $(short "$old")"
    git reset --hard --quiet "$old"
    if deps_outdated "$new" "$old"; then
        install_deps || log "Не удалось вернуть прежние зависимости"
    fi
    # Если до перезапуска дело не дошло (упали тесты), старый бот так и работает.
    if [[ $BOT_RESTARTED == 1 ]]; then
        restart_bot || log "Предыдущая версия тоже не запускается, значит дело, скорее всего, не в коде." \
            "Проверь .env, сеть и лог: journalctl --user -u $BOT_SERVICE"
    fi
}

main() {
    case "${1:-}" in
        "") ;;
        --force) FORCE=1 ;;
        -h | --help)
            usage
            return 0
            ;;
        *)
            usage >&2
            die "неизвестный аргумент: $1"
            ;;
    esac

    cd "$REPO_DIR"
    mkdir -p "$STATE_DIR"
    exec 9>"$STATE_DIR/lock"
    if ! flock --nonblock 9; then
        log "Обновление уже идёт в другом процессе, пропускаю"
        return 0
    fi

    [[ -x $PY ]] || die "не найдено виртуальное окружение $VENV_DIR. Сначала запусти deploy/install.sh"

    local branch
    branch="${DEPLOY_BRANCH:-$(git symbolic-ref --quiet --short HEAD || true)}"
    [[ -n $branch ]] || die "клон не стоит на ветке (detached HEAD). Переключись: git switch <ветка>"

    git fetch --quiet "$REMOTE" "$branch" ||
        die "не удалось скачать $REMOTE/$branch: нет сети или доступа к репозиторию"

    local old new
    old="$(git rev-parse HEAD)"
    new="$(git rev-parse FETCH_HEAD)"

    if [[ $FORCE == 0 ]]; then
        if [[ $old == "$new" ]]; then return 0; fi
        if [[ -f $FAILED_FILE && "$(<"$FAILED_FILE")" == "$new" ]]; then return 0; fi
    fi

    if ! git diff --quiet || ! git diff --cached --quiet; then
        die "на сервере изменены файлы репозитория (см. git status)." \
            "Закоммить их или убери в сторону (git stash), иначе обновление невозможно"
    fi
    if ! git merge-base --is-ancestor "$old" "$new"; then
        die "ветка на сервере разошлась с $REMOTE/$branch (локальные коммиты или force-push)." \
            "Проверь git log; если локальные коммиты не нужны: git reset --hard $REMOTE/$branch"
    fi

    if [[ $old == "$new" ]]; then
        log "Переустанавливаю версию $(short "$new") (--force)"
    else
        log "Новая версия $(short "$old") → $(short "$new"):"
        git log --max-count=20 --format='  %h %s' "$old..$new"
        git merge --ff-only --quiet "$new"
    fi

    if deploy "$old" "$new"; then
        rm -f "$FAILED_FILE"
        log "Готово: бот работает на версии $(short "$new")"
        return 0
    fi

    printf '%s\n' "$new" >"$FAILED_FILE"
    if [[ $old == "$new" ]]; then
        die "версия $(short "$new") не прошла проверку, откатываться не на что"
    fi
    log "Версия $(short "$new") не прошла проверку"
    rollback "$old" "$new"
    die "обновление отменено, возвращена версия $(short "$old")." \
        "Коммит $(short "$new") автоматически больше ставиться не будет: исправь ошибку и запушь" \
        "новый коммит или запусти deploy/update.sh --force"
}

# Весь код внутри main, а вызов и exit на одной строке: bash дочитывает файл по ходу
# выполнения, а git merge может заменить этот скрипт новой версией прямо во время работы.
main "$@"; exit $?
