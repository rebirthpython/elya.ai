#!/data/data/com.termux/files/usr/bin/bash
# Запуск Эли на телефоне (Termux) со «сторожем»: поднимает туннель Cloudflare и бота
# и перезапускает их сами, если бот упал, туннель оборвался или перестал отвечать снаружи
# (ошибка Cloudflare 1033). Адрес туннеля при каждом перезапуске новый — бот сам обновляет
# кнопку меню. Остановить: CTRL+C.
cd "$(dirname "$0")/.." || exit 1

[ -f .env ] || { echo "Нет файла .env — создай его (см. README, раздел «Телефон»)."; exit 1; }
[ -f .venv/bin/activate ] || { echo "Нет .venv — сначала выполни установку (python -m venv .venv ...)."; exit 1; }
source .venv/bin/activate

# Не даём Android усыпить Termux (нужен termux-api; если его нет — просто пропускаем).
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

PORT="${WEBAPP_PORT:-8080}"
CLOUDFLARED="${CLOUDFLARED:-cloudflared}"
PYTHON="${PYTHON:-python}"
POLL="${POLL:-5}"                        # как часто проверять, живы ли процессы, сек
HEALTH_GRACE="${HEALTH_GRACE:-90}"       # сколько ждать после запуска до первой проверки снаружи
HEALTH_INTERVAL="${HEALTH_INTERVAL:-60}" # как часто проверять адрес снаружи; 0 — не проверять
HEALTH_FAILS="${HEALTH_FAILS:-3}"        # сколько неудач подряд — повод перезапустить
MAX_DELAY="${MAX_DELAY:-60}"             # предел паузы между попытками, сек

LOG="$(mktemp)"
TUNNEL_PID=""
BOT_PID=""
URL=""

say() { echo "[$(date +%H:%M:%S)] $*"; }

# Мягко останавливает процесс, через 10 секунд — принудительно.
stop_pid() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null || return 0
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null; return 0; }
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
}

stop_children() {
  stop_pid "$BOT_PID"
  stop_pid "$TUNNEL_PID"
  BOT_PID=""
  TUNNEL_PID=""
}

cleanup() {
  stop_children
  rm -f "$LOG"
}
trap 'cleanup; say "Эля остановлена."; exit 0' INT TERM
trap cleanup EXIT

start_tunnel() {
  : >"$LOG"
  "$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" >"$LOG" 2>&1 &
  TUNNEL_PID=$!
  URL=""
  for _ in $(seq 1 60); do
    URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | grep -v '://api\.' | head -n 1)"
    [ -n "$URL" ] && return 0
    kill -0 "$TUNNEL_PID" 2>/dev/null || break
    sleep 1
  done
  say "Не удалось получить адрес туннеля (нет интернета?). Последние строки лога cloudflared:"
  tail -n 5 "$LOG"
  return 1
}

# Следит за ботом и туннелем; возвращается, когда что-то из них надо перезапустить.
watch_children() {
  local started fails next_check now code
  started=$(date +%s)
  fails=0
  next_check=$((started + HEALTH_GRACE))
  while :; do
    sleep "$POLL" &
    wait $!
    if ! kill -0 "$BOT_PID" 2>/dev/null; then
      wait "$BOT_PID" 2>/dev/null
      code=$?
      BOT_PID=""
      REASON="бот остановился (код $code)"
      [ $(( $(date +%s) - started )) -lt 30 ] && \
        REASON="$REASON — если повторяется, посмотри ошибку выше (часто это .env или нет интернета)"
      return
    fi
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
      REASON="туннель оборвался"
      return
    fi
    now=$(date +%s)
    if [ "$HEALTH_INTERVAL" -gt 0 ] && [ "$now" -ge "$next_check" ] && command -v curl >/dev/null 2>&1; then
      next_check=$((now + HEALTH_INTERVAL))
      if curl -fsS -m 15 -o /dev/null "$URL/healthz" 2>/dev/null; then
        fails=0
      else
        fails=$((fails + 1))
        say "Адрес приложения не отвечает снаружи ($fails из $HEALTH_FAILS)"
        if [ "$fails" -ge "$HEALTH_FAILS" ]; then
          REASON="туннель не отвечает"
          return
        fi
      fi
    fi
  done
}

delay=5
while :; do
  cycle_started=$(date +%s)
  REASON="не удалось поднять туннель"
  if start_tunnel; then
    say "Адрес приложения: $URL"
    # Переменные окружения важнее .env, поэтому адрес из туннеля переопределяет WEBAPP_URL.
    WEBAPP_URL="$URL" WEBAPP_HOST=127.0.0.1 WEBAPP_PORT="$PORT" "$PYTHON" main.py &
    BOT_PID=$!
    watch_children
  fi
  stop_children
  # Проработали больше 5 минут — сбой случайный, перезапускаемся быстро;
  # падаем раз за разом — пауза растёт, чтобы не крутиться впустую без интернета.
  [ $(( $(date +%s) - cycle_started )) -ge 300 ] && delay=5
  say "Перезапуск: $REASON. Новая попытка через ${delay} с…"
  sleep "$delay" &
  wait $!
  delay=$((delay * 2))
  [ "$delay" -gt "$MAX_DELAY" ] && delay="$MAX_DELAY"
done
