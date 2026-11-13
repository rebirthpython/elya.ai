#!/data/data/com.termux/files/usr/bin/bash
# Запуск Эли на телефоне (Termux): поднимает туннель Cloudflare и бота с нужным адресом.
# Адрес туннеля меняется при каждом запуске, поэтому подставляется автоматически.
cd "$(dirname "$0")/.." || exit 1

[ -f .env ] || { echo "Нет файла .env — создай его (см. README, раздел «Телефон»)."; exit 1; }
[ -f .venv/bin/activate ] || { echo "Нет .venv — сначала выполни установку (python -m venv .venv ...)."; exit 1; }
source .venv/bin/activate

# Не даём Android усыпить Termux (нужен termux-api; если его нет — просто пропускаем).
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

PORT="${WEBAPP_PORT:-8080}"
LOG="$(mktemp)"

cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" >"$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null; rm -f "$LOG"' EXIT

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | grep -v '://api\.' | head -n 1)"
  [ -n "$URL" ] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "Не удалось получить адрес туннеля. Лог cloudflared:"
  cat "$LOG"
  exit 1
fi

echo "Адрес приложения: $URL"
# Переменные окружения важнее .env, поэтому адрес из туннеля переопределяет WEBAPP_URL.
export WEBAPP_URL="$URL" WEBAPP_HOST=127.0.0.1 WEBAPP_PORT="$PORT"
python main.py
