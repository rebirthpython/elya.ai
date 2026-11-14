/* Эля — логика Mini App: вход через Telegram, лента сообщений, отправка, настройки. */
(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const query = new URLSearchParams(window.location.search);
  // ?demo — просмотр интерфейса в обычном браузере: ответы-заготовки, сервер не нужен.
  const DEMO = query.has('demo');
  const IN_TELEGRAM = Boolean(tg && tg.initData);

  const AUTO = 'auto';
  const MODEL_KEY = 'elya.model';
  const THEME_KEY = 'elya.theme';
  const STYLE_EMOJI = { friendly: '😊', caring: '🤗', playful: '😜', business: '💼' };
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const STICK_DISTANCE = 90;

  const $ = (id) => document.getElementById(id);
  const dom = {
    app: $('app'),
    topbar: $('topbar'),
    heroOrb: $('hero-orb'),
    avatar: $('avatar'),
    assistantName: $('assistant-name'),
    status: $('status'),
    statusLabel: $('status-label'),
    menuBtn: $('menu-btn'),
    chat: $('chat'),
    welcome: $('welcome'),
    welcomeTitle: $('welcome-title'),
    welcomeName: $('welcome-name'),
    suggestions: $('suggestions'),
    messages: $('messages'),
    scrollDown: $('scroll-down'),
    composer: document.querySelector('.composer'),
    composerBox: $('composer-box'),
    attach: $('attach'),
    fileInput: $('file-input'),
    attachment: $('attachment'),
    attachmentImg: $('attachment-img'),
    attachmentRemove: $('attachment-remove'),
    mic: $('mic'),
    recorder: $('recorder'),
    recCancel: $('rec-cancel'),
    recTime: $('rec-time'),
    recHint: $('rec-hint'),
    input: $('input'),
    counter: $('counter'),
    send: $('send'),
    hint: $('hint'),
    backdrop: $('sheet-backdrop'),
    sheet: $('sheet'),
    sheetClose: $('sheet-close'),
    sheetGrip: $('sheet-grip'),
    sheetHead: $('sheet-head'),
    modelList: $('model-list'),
    themeControl: $('theme-control'),
    styleList: $('style-list'),
    verbosityControl: $('verbosity-control'),
    verbosityNote: $('verbosity-note'),
    memory: document.querySelector('.memory'),
    memoryToggle: $('memory-toggle'),
    memorySub: $('memory-sub'),
    memoryBody: $('memory-body'),
    facts: $('facts'),
    factsEmpty: $('facts-empty'),
    factForm: $('fact-form'),
    factInput: $('fact-input'),
    factAdd: $('fact-add'),
    factsClear: $('facts-clear'),
    factsClearLabel: $('facts-clear-label'),
    bellBtn: $('bell-btn'),
    bellBadge: $('bell-badge'),
    sheetBody: $('sheet-body'),
    sheetBack: $('sheet-back'),
    sheetTitle: $('sheet-title'),
    instructionsBlock: $('instructions-block'),
    openInstructions: $('open-instructions'),
    instructionsSummary: $('instructions-summary'),
    instructionsPage: $('instructions-page'),
    instructionsToggle: $('instructions-toggle'),
    instructionsStatus: $('instructions-status'),
    instrAbout: $('instr-about'),
    instrStyle: $('instr-style'),
    instrAboutCount: $('instr-about-count'),
    instrStyleCount: $('instr-style-count'),
    instrSave: $('instr-save'),
    remindersBlock: $('reminders-block'),
    remindersList: $('reminders-list'),
    remindersEmpty: $('reminders-empty'),
    reminderForm: $('reminder-form'),
    reminderText: $('reminder-text'),
    reminderWhen: $('reminder-when'),
    reminderRepeat: $('reminder-repeat'),
    reminderAdd: $('reminder-add'),
    tzNote: $('tz-note'),
    usageValue: $('usage-value'),
    usageLimit: $('usage-limit'),
    usageBar: $('usage-bar'),
    usageFill: $('usage-fill'),
    clearBtn: $('clear-btn'),
    clearConfirm: $('clear-confirm'),
    clearCancel: $('clear-cancel'),
    clearYes: $('clear-yes'),
    privacy: $('privacy-text'),
    toast: $('toast'),
    gate: $('gate'),
    gateOrb: document.querySelector('#gate .orb'),
    gateTitle: $('gate-title'),
    gateText: $('gate-text'),
    gateBtn: $('gate-btn'),
  };

  const state = {
    assistantName: 'Эля',
    firstName: '',
    models: [],
    providers: [],
    usage: { used: 0, limit: 0 },
    maxLength: 4000,
    model: AUTO,
    busy: false,
    stick: true,
    lastDay: null,
    signature: '',
    syncedAt: 0,
    finishReveal: null,
    notice: null,
    gateAction: null,
    touching: false,
    wheelAt: 0,
    sheetAnim: null,
    sheetClosing: false,
    theme: 'auto',
    preferences: { style: 'friendly', verbosity: 'normal' },
    preferenceOptions: { style: [], verbosity: [] },
    memory: { enabled: false, facts: [], max_facts: 50, max_fact_length: 200 },
    memoryTimers: [],
    voice: false,
    vision: false,
    attachment: null,
    recording: null,
    transcribing: false,
    reminders: { timezone: null, items: [], max: 50, max_text: 300 },
    instructions: null,
    instructionsEdited: false, // пользователь правил поля и ещё не сохранил
    sheetPage: 'main',
    clearArmed: null,
  };

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  /* ---------- Мелкие помощники ---------- */

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(id) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + id);
    svg.setAttribute('aria-hidden', 'true');
    svg.append(use);
    return svg;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const SPRING = 'cubic-bezier(0.22, 1.2, 0.36, 1)';
  const isWide = () => window.matchMedia('(min-width: 640px)').matches;

  /* Web Animations с учётом «меньше движения»: возвращает анимацию или null. */
  function motion(node, keyframes, options) {
    if (reducedMotion || !node || typeof node.animate !== 'function') return null;
    return node.animate(keyframes, options);
  }

  function countUp(node, to) {
    if (reducedMotion || to <= 0) {
      node.textContent = String(to);
      return;
    }
    const started = performance.now();
    const duration = 750;
    (function step(now) {
      const t = Math.min(1, (now - started) / duration);
      node.textContent = String(Math.round(to * (1 - Math.pow(1 - t, 3))));
      if (t < 1) requestAnimationFrame(step);
    })(started);
  }
  const nowTs = () => Math.floor(Date.now() / 1000);

  function supports(version) {
    return Boolean(IN_TELEGRAM && tg.isVersionAtLeast && tg.isVersionAtLeast(version));
  }

  function haptic(kind, value) {
    if (!supports('6.1') || !tg.HapticFeedback) return;
    try {
      if (kind === 'impact') tg.HapticFeedback.impactOccurred(value || 'light');
      else if (kind === 'notify') tg.HapticFeedback.notificationOccurred(value || 'success');
      else tg.HapticFeedback.selectionChanged();
    } catch (error) { /* вибрация — не критично */ }
  }

  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch (error) { /* хранилище может быть недоступно */ }
    return null;
  }

  /* ---------- Тема ---------- */

  function resolveScheme() {
    const forced = query.get('theme');
    if (forced === 'light' || forced === 'dark') return forced;
    if (state.theme === 'light' || state.theme === 'dark') return state.theme;
    if (IN_TELEGRAM) return tg.colorScheme === 'light' ? 'light' : 'dark';
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }

  function paintTheme(scheme) {
    const root = document.documentElement;
    root.dataset.theme = scheme;
    const bg = getComputedStyle(root).getPropertyValue('--bg').trim();
    try {
      if (supports('6.9')) tg.setHeaderColor(bg);
      if (supports('6.1')) tg.setBackgroundColor(bg);
      if (supports('7.10')) tg.setBottomBarColor(bg);
    } catch (error) { /* старый клиент Telegram */ }
  }

  let themeFadeTimer = null;

  /* origin — точка {x, y}, из которой расходится новая тема; без неё тема меняется сразу. */
  function applyTheme(origin) {
    const root = document.documentElement;
    const scheme = resolveScheme();
    if (root.dataset.themeReady && root.dataset.theme === scheme) return;
    root.dataset.themeReady = '1';

    if (!origin || reducedMotion) {
      paintTheme(scheme);
      return;
    }

    if (typeof document.startViewTransition === 'function') {
      const { x, y } = origin;
      const radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
      const transition = document.startViewTransition(() => paintTheme(scheme));
      transition.ready.then(() => {
        root.animate(
          { clipPath: ['circle(0px at ' + x + 'px ' + y + 'px)', 'circle(' + radius + 'px at ' + x + 'px ' + y + 'px)'] },
          { duration: 700, easing: 'cubic-bezier(0.4, 0, 0.2, 1)', pseudoElement: '::view-transition-new(root)' }
        );
      }).catch(() => {});
      return;
    }

    // Без View Transitions цвета просто плавно перетекают.
    root.classList.add('theme-fade');
    paintTheme(scheme);
    clearTimeout(themeFadeTimer);
    themeFadeTimer = setTimeout(() => root.classList.remove('theme-fade'), 700);
  }

  /* ---------- Сервер ---------- */

  class ApiError extends Error {
    constructor(code, message, status) {
      super(message);
      this.code = code;
      this.status = status || 0;
    }
  }

  async function request(path, body, rawType) {
    let response;
    try {
      response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: Object.assign(
          { Authorization: 'tma ' + tg.initData },
          // FormData сама выставляет multipart-заголовок с границей
          body === undefined || body instanceof FormData ? {} : { 'Content-Type': rawType || 'application/json' }
        ),
        body: body === undefined ? undefined : rawType || body instanceof FormData ? body : JSON.stringify(body),
      });
    } catch (error) {
      throw new ApiError('network', 'Нет связи с сервером. Проверь интернет и попробуй ещё раз.');
    }

    let data = null;
    try {
      data = await response.json();
    } catch (error) { /* не JSON — разберёмся по статусу */ }

    if (!response.ok || data === null) {
      const info = (data && data.error) || {};
      throw new ApiError(
        info.code || 'http_' + response.status,
        info.message || 'Сервер ответил ошибкой. Попробуй ещё раз чуть позже.',
        response.status
      );
    }
    return data;
  }

  const server = {
    state: () => request('api/state'),
    chat: (text, model) => request('api/chat', model === AUTO ? { text } : { text, model }),
    /* Ответ по мере генерации (NDJSON). onDelta получает новые куски текста;
       возвращает то же, что chat(). Без поддержки потоков — обычный chat(). */
    async chatStream(text, model, onDelta) {
      if (!window.ReadableStream || !window.TextDecoder) return server.chat(text, model);
      let response;
      try {
        response = await fetch('api/chat/stream', {
          method: 'POST',
          headers: { Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json' },
          body: JSON.stringify(model === AUTO ? { text } : { text, model }),
        });
      } catch (error) {
        throw new ApiError('network', 'Нет связи с сервером. Проверь интернет и попробуй ещё раз.');
      }
      if (!response.ok || !response.body) {
        let data = null;
        try {
          data = await response.json();
        } catch (error) { /* не JSON */ }
        const info = (data && data.error) || {};
        throw new ApiError(info.code || 'http_' + response.status,
          info.message || 'Сервер ответил ошибкой. Попробуй ещё раз чуть позже.', response.status);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let result = null;
      const handle = (line) => {
        if (!line.trim()) return;
        let event;
        try {
          event = JSON.parse(line);
        } catch (error) {
          return;
        }
        if (event.type === 'delta' && event.text) onDelta(event.text);
        else if (event.type === 'done') result = event;
        else if (event.type === 'error') {
          const info = event.error || {};
          throw new ApiError(info.code || 'ai_unavailable', info.message || 'Ответ оборвался. Попробуй ещё раз.', 503);
        }
      };
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let newline;
          while ((newline = buffer.indexOf('\n')) !== -1) {
            handle(buffer.slice(0, newline));
            buffer = buffer.slice(newline + 1);
          }
        }
        handle(buffer + decoder.decode());
      } catch (error) {
        if (error instanceof ApiError) throw error;
        throw new ApiError('network', 'Связь прервалась посреди ответа. Попробуй ещё раз.');
      }
      if (!result) throw new ApiError('network', 'Связь прервалась посреди ответа. Попробуй ещё раз.');
      return result;
    },
    clear: () => request('api/clear', {}),
    savePreferences: (patch) => request('api/preferences', patch),
    saveInstructions: (patch) => request('api/instructions', patch),
    memorySet: (enabled) => request('api/memory', { enabled }),
    memoryAdd: (text) => request('api/memory/add', { text }),
    memoryDelete: (id) => request('api/memory/delete', { id }),
    memoryClear: () => request('api/memory/delete', { all: true }),
    setTimezone: (tz) => request('api/timezone', { tz }),
    voice: (blob, type) => request('api/voice', blob, type),
    photo: (text, blob, model) => {
      const form = new FormData();
      if (text) form.append('text', text);
      if (model && model !== AUTO) form.append('model', model);
      form.append('image', blob, 'photo.jpg');
      return request('api/photo', form);
    },
    reminderAdd: (text, when, repeat) => request('api/reminders/add', { text, when, repeat }),
    reminderDelete: (id) => request('api/reminders/delete', { id }),
  };

  /* Демо-режим: тот же интерфейс, но без сервера и настоящей модели. */
  const demo = (function () {
    const replies = [
      'Привет! Я Эля 👋\n\nСейчас я в **демо-режиме** и отвечаю заготовками, чтобы показать, как выглядит приложение. В Telegram за меня думает настоящая языковая модель.',
      'Вот как я оформляю ответы:\n\n1. **Списки** — когда шагов несколько\n2. *Акценты* — чтобы выделить главное\n3. `Код` — если речь о технике\n\n> Важное лучше перепроверять: я ИИ и могу ошибаться.',
      'Пример с кодом:\n\n```python\ndef greet(name: str) -> str:\n    return f"Привет, {name}!"\n\nprint(greet("мир"))\n```\n\nКнопка в углу блока скопирует его целиком.',
      'Давай набросаем план на день:\n\n- **Утро** — самая сложная задача, пока голова свежая\n- **День** — встречи и переписка\n- **Вечер** — спорт, прогулка или книга\n\nРасскажи, что у тебя в списке дел, и я разложу всё по времени.',
    ];
    const sample = [
      { role: 'user', content: 'Привет! Что ты умеешь?', ago: 3600 * 26 },
      { role: 'assistant', content: 'Привет! Я Эля — твой ИИ-ассистент. Вот чем могу помочь:\n\n- **Ответить на вопрос** и объяснить сложное простыми словами\n- **Спланировать** день, неделю или проект\n- **Написать и отредактировать** текст: сообщение, пост, письмо\n- Просто *поболтать*, если хочется компании\n\nС чего начнём?', ago: 3600 * 26 - 8 },
      { role: 'user', content: 'Как быстро посчитать сумму чисел от 1 до 100?', ago: 420 },
      { role: 'assistant', content: 'Есть красивый приём, который приписывают Гауссу: складываем числа парами с концов.\n\n1. `1 + 100 = 101`\n2. `2 + 99 = 101`\n3. …и так **50 пар**\n\nЗначит, сумма равна `50 × 101 = 5050`.\n\nА в коде это одна строка:\n\n```python\nprint(sum(range(1, 101)))  # 5050\n```', ago: 400 },
    ];
    let history = query.get('demo') === 'chat'
      ? sample.map((m) => ({ role: m.role, content: m.content, ts: nowTs() - m.ago }))
      : [];
    let used = 12;
    let turn = 0;
    let preferences = { style: 'friendly', verbosity: 'normal' };
    let instructions = { about: '', style: '', enabled: true, max_length: 1500 };
    let memory = { enabled: false, facts: [], max_facts: 50, max_fact_length: 200 };
    let factId = 0;
    let reminders = { timezone: null, items: [], max: 50, max_text: 300 };
    let reminderId = 0;
    const remindersView = () => JSON.parse(JSON.stringify(reminders));
    const memoryView = () => JSON.parse(JSON.stringify(memory));

    return {
      async state() {
        await sleep(450);
        return {
          assistant: { name: 'Эля' },
          user: { first_name: (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.first_name) || '' },
          history: history.slice(),
          models: [
            { id: 'gemini:gemini-flash-latest', provider: 'Google Gemini API', model: 'gemini-flash-latest' },
            { id: 'openrouter:meta-llama/llama-3.3-70b-instruct:free', provider: 'OpenRouter', model: 'meta-llama/llama-3.3-70b-instruct:free' },
            { id: 'groq:llama-3.3-70b-versatile', provider: 'Groq', model: 'llama-3.3-70b-versatile' },
          ],
          providers: ['Google Gemini API', 'OpenRouter', 'Groq'],
          usage: { used, limit: 300 },
          limits: { max_text_length: 4000 },
          preferences: Object.assign({}, preferences),
          instructions: Object.assign({}, instructions),
          memory: memoryView(),
          reminders: remindersView(),
          voice: true,
          vision: true,
          preference_options: {
            style: [
              { id: 'friendly', title: 'Дружелюбный', description: 'Тепло, с лёгким юмором' },
              { id: 'caring', title: 'Заботливый', description: 'Мягко, с поддержкой и вниманием' },
              { id: 'playful', title: 'Весёлый', description: 'Шутки, игра слов и эмодзи' },
              { id: 'business', title: 'Деловой', description: 'Сдержанно и по существу, на «вы»' },
            ],
            verbosity: [
              { id: 'short', title: 'Кратко', description: '1–3 предложения' },
              { id: 'normal', title: 'Обычно', description: 'По ситуации' },
              { id: 'detailed', title: 'Подробно', description: 'С объяснениями и примерами' },
            ],
          },
        };
      },
      async saveInstructions(patch) {
        await sleep(250);
        instructions = Object.assign({}, instructions, patch);
        return Object.assign({}, instructions);
      },
      async savePreferences(patch) {
        await sleep(250);
        preferences = Object.assign({}, preferences, patch);
        return { preferences: Object.assign({}, preferences) };
      },
      async memorySet(enabled) {
        await sleep(200);
        memory.enabled = enabled;
        return memoryView();
      },
      async memoryAdd(text) {
        await sleep(200);
        const value = String(text).trim();
        if (value.length < 3) throw new ApiError('too_short', 'Слишком коротко: напиши факт хотя бы в паре слов.', 400);
        if (/парол|карт/i.test(value)) throw new ApiError('sensitive', 'Пароли, номера карт и ключи я не запоминаю.', 400);
        factId += 1;
        const added = { id: factId, text: value };
        memory.facts.push(added);
        return Object.assign(memoryView(), { added });
      },
      async memoryDelete(id) {
        await sleep(150);
        memory.facts = memory.facts.filter((fact) => fact.id !== id);
        return memoryView();
      },
      async photo(text) {
        await sleep(1200);
        used += 1;
        const content = 'Вижу фото! 📷 В демо-режиме я не смотрю картинки по-настоящему, а в Telegram модель опишет, что на нём' +
          (text ? ', и ответит на вопрос: «' + text + '»' : '') + '.';
        history.push({ role: 'user', content: text ? '📷 Фото: ' + text : '📷 Фото', ts: nowTs() }, { role: 'assistant', content, ts: nowTs() });
        return { reply: { role: 'assistant', content, ts: nowTs(), provider: 'demo', model: 'gemini-flash-latest', truncated: false }, usage: { used, limit: 300 } };
      },
      async voice() {
        await sleep(700);
        return { text: 'Привет! Это голосовое в демо-режиме' };
      },
      async setTimezone(tz) {
        reminders.timezone = tz;
        return remindersView();
      },
      async reminderAdd(text, when, repeat) {
        await sleep(200);
        const due = Math.floor(new Date(when).getTime() / 1000);
        if (!(due > nowTs())) throw new ApiError('past', 'Это время уже прошло. Выбери время в будущем.', 400);
        reminderId += 1;
        reminders.items.push({ id: reminderId, text, due, repeat });
        reminders.items.sort((a, b) => a.due - b.due);
        return remindersView();
      },
      async reminderDelete(id) {
        await sleep(150);
        reminders.items = reminders.items.filter((item) => item.id !== id);
        return remindersView();
      },
      async memoryClear() {
        await sleep(150);
        memory.facts = [];
        return memoryView();
      },
      async chatStream(text, model, onDelta) {
        const data = await this.chat(text, model);
        if (data.reply.provider === 'reminders') return data;
        const words = data.reply.content.split(/(\s+)/);
        for (let i = 0; i < words.length; i += 4) {
          onDelta(words.slice(i, i + 4).join(''));
          await sleep(60);
        }
        return data;
      },
      async chat(text, model) {
        await sleep(1100 + Math.random() * 700);
        if (/ошибк/i.test(text)) {
          throw new ApiError('ai_unavailable', 'ИИ-сервис сейчас не отвечает. Отправь сообщение ещё раз чуть позже.', 503);
        }
        if (/напомни/i.test(text)) {
          reminderId += 1;
          reminders.items.push({ id: reminderId, text: text.replace(/^.*?напомни\S*\s*/i, '') || 'Напоминание', due: nowTs() + 3600, repeat: 'none' });
          const content = 'Хорошо, напомню через 1 ч: ' + text.replace(/^.*?напомни\S*\s*/i, '') + ' ⏰\nВсе напоминания — в /reminders или в приложении (кнопка 🔔).';
          history.push({ role: 'user', content: text, ts: nowTs() }, { role: 'assistant', content, ts: nowTs() });
          return { reply: { role: 'assistant', content, ts: nowTs(), provider: 'reminders', model: '', truncated: false }, usage: { used, limit: 300 } };
        }
        const content = replies[turn % replies.length];
        turn += 1;
        used += 1;
        if (memory.enabled && /(^|\s)(я|мне|люблю|живу)(\s|$)/i.test(text)) {
          setTimeout(() => {
            factId += 1;
            memory.facts.push({ id: factId, text: 'Рассказал: ' + text.slice(0, 50) });
          }, 1200);
        }
        history.push({ role: 'user', content: text, ts: nowTs() }, { role: 'assistant', content, ts: nowTs() });
        return {
          reply: {
            role: 'assistant',
            content,
            ts: nowTs(),
            provider: 'demo',
            model: model === AUTO ? 'gemini-flash-latest' : model.slice(model.indexOf(':') + 1),
            truncated: false,
          },
          usage: { used, limit: 300 },
        };
      },
      async clear() {
        await sleep(300);
        const deleted = history.length;
        history = [];
        return { deleted };
      },
    };
  })();

  const backend = DEMO ? demo : server;

  /* ---------- Экран состояний ---------- */

  function showGate(title, text, button) {
    dom.gateTitle.textContent = title;
    dom.gateText.textContent = text || '';
    dom.gateOrb.classList.toggle('orb--thinking', !text && !button);
    state.gateAction = button ? button.action : null;
    dom.gateBtn.hidden = !button;
    if (button) dom.gateBtn.textContent = button.label;
    dom.gate.classList.remove('is-leaving');
    dom.gate.hidden = false;
  }

  function hideGate() {
    if (dom.gate.hidden) return;
    dom.gate.classList.add('is-leaving');
    setTimeout(() => { dom.gate.hidden = true; }, reducedMotion ? 0 : 560);
  }

  function showLoadError(error, retry) {
    dom.app.hidden = true;
    if (error.status === 401) {
      showGate('Не получилось войти', error.message,
        IN_TELEGRAM ? { label: 'Закрыть', action: () => tg.close() } : null);
    } else if (error.status === 403) {
      showGate('Доступ закрыт', error.message);
    } else {
      showGate('Не получилось загрузиться', error.message, { label: 'Попробовать снова', action: retry });
    }
  }

  /* ---------- Время и даты ---------- */

  const timeFormat = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const dayFormat = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long' });
  const dayYearFormat = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });

  function dayKey(date) {
    return date.getFullYear() + '-' + date.getMonth() + '-' + date.getDate();
  }

  function dayLabel(date) {
    const today = new Date();
    const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
    if (dayKey(date) === dayKey(today)) return 'Сегодня';
    if (dayKey(date) === dayKey(yesterday)) return 'Вчера';
    return (date.getFullYear() === today.getFullYear() ? dayFormat : dayYearFormat).format(date);
  }

  function greeting() {
    const hour = new Date().getHours();
    let phrase = 'Доброй ночи';
    if (hour >= 5 && hour < 12) phrase = 'Доброе утро';
    else if (hour >= 12 && hour < 18) phrase = 'Добрый день';
    else if (hour >= 18 && hour < 23) phrase = 'Добрый вечер';
    return state.firstName ? phrase + ', ' + state.firstName + '!' : phrase + '!';
  }

  /* ---------- Прокрутка ---------- */

  function distanceFromBottom() {
    return dom.chat.scrollHeight - dom.chat.scrollTop - dom.chat.clientHeight;
  }

  function scrollToBottom(smooth) {
    state.stick = true;
    dom.chat.scrollTo({ top: dom.chat.scrollHeight, behavior: smooth && !reducedMotion ? 'smooth' : 'auto' });
  }

  function onScroll() {
    const distance = distanceFromBottom();
    state.stick = distance < STICK_DISTANCE;
    const showDown = distance > 320;
    dom.scrollDown.classList.toggle('is-visible', showDown);
    dom.scrollDown.tabIndex = showDown ? 0 : -1;
    dom.topbar.classList.toggle('is-scrolled', dom.chat.scrollTop > 8);
  }

  /* Пока пользователь сам листает ленту, не тянем её вниз. */
  function userIsScrolling() {
    return state.touching || Date.now() - state.wheelAt < 300;
  }

  /* ---------- Сообщения ---------- */

  function modelTitle(model) {
    return String(model).split('/').pop().replace(/:free$/, '');
  }

  function setStatus(kind) {
    const labels = { online: 'на связи', typing: 'печатает…', offline: 'нет связи' };
    if (dom.status.dataset.state !== kind) {
      motion(dom.statusLabel, [
        { opacity: 0, transform: 'translateY(6px)' },
        { opacity: 1, transform: 'none' },
      ], { duration: 320, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
    }
    dom.status.dataset.state = kind;
    dom.statusLabel.textContent = labels[kind];
    dom.avatar.classList.toggle('orb--thinking', kind === 'typing');
    document.body.classList.toggle('is-thinking', kind === 'typing');
  }

  /* Заголовок разбивается на слова, чтобы они появлялись по очереди. */
  function setWelcomeTitle(text) {
    const parts = [];
    text.split(' ').forEach((word, index) => {
      if (index) parts.push(' ');
      const span = el('span', 'word', word);
      span.style.setProperty('--i', index);
      parts.push(span);
    });
    dom.welcomeTitle.replaceChildren(...parts);
  }

  let welcomeExit = null;

  function updateWelcome() {
    const empty = dom.messages.childElementCount === 0;
    if (empty) {
      if (welcomeExit) {
        welcomeExit.cancel();
        welcomeExit = null;
        dom.welcome.hidden = true;
      }
      if (dom.welcome.hidden) {
        setWelcomeTitle(greeting());
        dom.welcome.hidden = false;
      }
      return;
    }
    if (dom.welcome.hidden || welcomeExit) return;
    // Приветствие растворяется, пока появляется первое сообщение.
    welcomeExit = motion(dom.welcome, [
      { opacity: 1, transform: 'none', filter: 'blur(0)' },
      { opacity: 0, transform: 'translateY(-18px) scale(0.95)', filter: 'blur(6px)' },
    ], { duration: 280, easing: 'cubic-bezier(0.5, 0, 0.75, 0)', fill: 'forwards' });
    if (!welcomeExit) {
      dom.welcome.hidden = true;
      return;
    }
    const exit = welcomeExit;
    exit.onfinish = () => {
      if (welcomeExit !== exit) return;
      dom.welcome.hidden = true;
      exit.cancel();
      welcomeExit = null;
    };
  }

  function addDay(ts, fresh) {
    const date = new Date(ts * 1000);
    const key = dayKey(date);
    if (key === state.lastDay) return;
    state.lastDay = key;
    dom.messages.append(el('div', 'day' + (fresh ? ' day--new' : ''), dayLabel(date)));
  }

  function copyChip(text, label) {
    const button = el('button', 'chip-btn');
    button.type = 'button';
    button.dataset.copy = text;
    button.append(icon('i-copy'), el('span', '', label));
    return button;
  }

  function addMessage(message, options) {
    const fresh = Boolean(options && options.fresh);
    addDay(message.ts, fresh);

    const flight = fresh && options && options.flight;
    const root = el('article', 'msg msg--' + message.role + (flight ? ' msg--flying' : fresh ? ' msg--new' : ''));
    const bubble = el('div', 'bubble');
    const meta = el('div', 'msg__meta');
    const time = el('time', '', timeFormat.format(new Date(message.ts * 1000)));
    time.dateTime = new Date(message.ts * 1000).toISOString();
    meta.append(time);

    if (message.role === 'assistant') {
      if (message.model) {
        const model = el('span', 'msg__model', modelTitle(message.model));
        model.title = message.model;
        meta.append(model);
      }
      meta.append(copyChip(message.content, 'Копировать'));
      if (!(options && options.reveal)) bubble.append(window.ElyaMarkdown.render(message.content));
    } else if (options && options.image) {
      const photo = el('img', 'bubble__photo');
      photo.src = options.image;
      photo.alt = 'Фото';
      bubble.append(photo);
      if (message.content) bubble.append(document.createTextNode(message.content));
      else bubble.classList.add('bubble--photo-only');
    } else {
      bubble.textContent = message.content;
    }

    root.append(bubble, meta);
    dom.messages.append(root);
    return { root, bubble };
  }

  /* Плавное «допечатывание» ответа: пузырь растёт, в конце мерцает курсор, лента
     следует за ответом. push(text) — текст, полученный к этому моменту (при потоке он
     растёт), end(text) — ответ целиком. Показ догоняет поток, не обгоняя его. */
  function createReveal(root, bubble, onDone) {
    let target = '';
    let ended = false;
    let streamed = false;
    let shown = 0;
    let timer = null;
    let frame = 0;
    let grow = null;
    let finished = false;
    root.classList.add('is-streaming');

    function render(source) {
      const from = bubble.offsetHeight;
      if (grow) grow.cancel();
      bubble.replaceChildren(window.ElyaMarkdown.render(source));
      const to = bubble.offsetHeight;
      grow = from && Math.abs(to - from) > 1
        ? motion(bubble, [{ height: from + 'px' }, { height: to + 'px' }], { duration: 200, easing: 'ease-out' })
        : null;
    }

    function pin() {
      if (state.stick && !userIsScrolling()) dom.chat.scrollTop = dom.chat.scrollHeight;
      if (!finished) frame = requestAnimationFrame(pin);
    }

    function complete() {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      timer = null;
      cancelAnimationFrame(frame);
      render(target);
      const settle = () => {
        root.classList.remove('is-streaming');
        if (state.stick && !userIsScrolling()) scrollToBottom(false);
      };
      if (grow) grow.onfinish = settle;
      else settle();
      onDone();
    }

    function tick() {
      timer = null;
      if (finished) return;
      if (shown >= target.length) {
        if (ended) complete();
        return; // ждём следующий кусок потока
      }
      const remaining = target.length - shown;
      // Готовый текст — ровно, за ~2,5 с; поток — догоняем полученное, не обгоняя его.
      const step = streamed ? Math.max(2, Math.ceil(remaining / 8)) : Math.max(3, Math.ceil(target.length / 90));
      shown = Math.min(target.length, shown + step);
      // Не обрываем слово посередине (если оно уже пришло целиком).
      for (let extra = 0; extra < 24 && shown < target.length && !/\s/.test(target[shown]); extra += 1) shown += 1;
      if (ended && shown >= target.length) {
        complete();
        return;
      }
      render(target.slice(0, shown));
      timer = setTimeout(tick, 28);
    }

    frame = requestAnimationFrame(pin);
    return {
      push(text) {
        streamed = true;
        target = text;
        if (!timer && !finished) tick();
      },
      end(text) {
        if (typeof text === 'string') target = text;
        ended = true;
        if (reducedMotion) complete();
        else if (!timer && !finished) tick();
      },
      flush() {
        ended = true;
        complete();
      },
    };
  }

  function reveal(root, bubble, text, onDone) {
    if (reducedMotion || text.length < 30) {
      bubble.replaceChildren(window.ElyaMarkdown.render(text));
      onDone();
      return null;
    }
    const view = createReveal(root, bubble, onDone);
    view.end(text);
    return view.flush;
  }

  function addAssistantReply(reply) {
    const item = addMessage(reply, { fresh: true, reveal: true });
    state.finishReveal = reveal(item.root, item.bubble, reply.content, () => {
      state.finishReveal = null;
      if (reply.truncated) addContinueButton();
    });
  }

  /* Пузырь ответа, который заполняется по мере прихода потока. */
  function startStreamingReply() {
    const item = addMessage({ role: 'assistant', content: '', ts: nowTs() }, { fresh: true, reveal: true });
    const live = { item, text: '', reply: null };
    live.view = createReveal(item.root, item.bubble, () => {
      state.finishReveal = null;
      if (live.reply && live.reply.truncated) addContinueButton();
    });
    state.finishReveal = live.view.flush;
    return live;
  }

  /* Ответ пришёл целиком: подпись модели и кнопка «Копировать» с полным текстом. */
  function finishStreamingReply(live, reply) {
    live.reply = reply;
    const meta = live.item.root.querySelector('.msg__meta');
    const copy = meta.querySelector('[data-copy]');
    if (copy) copy.dataset.copy = reply.content;
    if (reply.model) {
      const model = el('span', 'msg__model', modelTitle(reply.model));
      model.title = reply.model;
      meta.insertBefore(model, copy);
    }
    live.view.end(reply.content);
  }

  function addContinueButton() {
    const button = el('button', 'continue-btn msg--new', 'Продолжить ответ');
    button.type = 'button';
    button.addEventListener('click', () => {
      button.remove();
      send('Продолжи');
    });
    const stick = state.stick;
    dom.messages.append(button);
    if (stick) scrollToBottom(true);
  }

  const TYPING_HINTS = [
    [4000, 'думаю…'],
    [12000, 'подбираю слова…'],
    [30000, 'модель отвечает дольше обычного…'],
  ];

  function showTyping() {
    const root = el('article', 'msg msg--assistant msg--new typing');
    const bubble = el('div', 'bubble');
    const dots = el('span', 'typing__dots');
    dots.append(el('span'), el('span'), el('span'));
    bubble.append(dots);
    bubble.setAttribute('aria-label', state.assistantName + ' печатает');
    root.append(bubble);
    dom.messages.append(root);

    // Если модель долго думает, рядом с точками появляется пояснение.
    const timers = TYPING_HINTS.map(([delay, text]) => setTimeout(() => {
      const from = bubble.offsetWidth;
      let hint = bubble.querySelector('.typing__hint');
      if (!hint) {
        hint = el('span', 'typing__hint');
        bubble.append(hint);
      }
      hint.textContent = text;
      motion(hint, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 350, easing: 'ease-out' });
      motion(bubble, [{ width: from + 'px' }, { width: bubble.offsetWidth + 'px' }], { duration: 450, easing: SPRING });
    }, delay));
    root.dismiss = () => {
      timers.forEach(clearTimeout);
      root.remove();
    };
    return root;
  }

  function clearNotice() {
    if (state.notice) state.notice.remove();
    state.notice = null;
  }

  function showNotice(text, onRetry) {
    clearNotice();
    const box = el('div', 'notice msg--new');
    box.setAttribute('role', 'alert');
    const body = el('div', 'notice__body');
    body.append(el('p', 'notice__text', text));
    const retry = el('button', 'notice__retry');
    retry.type = 'button';
    retry.append(icon('i-retry'), el('span', '', 'Повторить'));
    retry.addEventListener('click', onRetry);
    body.append(retry);
    box.append(icon('i-alert'), body);
    dom.messages.append(box);
    state.notice = box;
  }

  function historySignature(history) {
    const last = history[history.length - 1];
    return history.length + ':' + (last ? last.ts + ':' + last.content.length : '');
  }

  function renderHistory(history) {
    if (state.finishReveal) state.finishReveal();
    dom.messages.replaceChildren();
    state.lastDay = null;
    state.notice = null;
    history.forEach((message) => addMessage(message));
    state.signature = historySignature(history);
    updateWelcome();
    scrollToBottom(false);
    onScroll();
  }

  /* ---------- Отправка ---------- */

  function setBusy(busy) {
    const was = state.busy;
    state.busy = busy;
    dom.send.classList.toggle('is-busy', busy);
    if (was && !busy && !reducedMotion) {
      // Стрелка возвращается на кнопку снизу.
      dom.send.classList.add('is-landing');
      setTimeout(() => dom.send.classList.remove('is-landing'), 600);
    }
    setStatus(busy ? 'typing' : 'online');
    updateComposer();
  }

  const CAN_RECORD = Boolean(
    navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder
  );

  function updateComposer() {
    const length = dom.input.value.length;
    const empty = !dom.input.value.trim() && !state.attachment;
    dom.send.disabled = state.recording ? false : state.busy || state.transcribing || empty;
    dom.composerBox.classList.toggle('can-attach', state.vision);
    dom.attach.disabled = state.busy;
    dom.composerBox.classList.toggle(
      'has-mic',
      state.voice && CAN_RECORD && empty && !state.recording && !state.transcribing
    );
    dom.mic.disabled = state.busy;
    const showCounter = length > state.maxLength * 0.85;
    dom.counter.hidden = !showCounter;
    if (showCounter) {
      dom.counter.textContent = length + ' / ' + state.maxLength;
      dom.counter.classList.toggle('is-over', length >= state.maxLength);
    }
  }

  function autosize() {
    dom.input.style.height = 'auto';
    dom.input.style.height = Math.min(dom.input.scrollHeight, 140) + 'px';
  }

  /* Отправленный текст летит из поля ввода (или с карточки-подсказки) на своё место в ленте. */
  function flyIn(item, origin) {
    const bubble = item.bubble;
    const target = bubble.getBoundingClientRect();
    const ghost = el('div', 'flight');
    ghost.append(el('span', 'flight__bg'), document.createTextNode(bubble.textContent));
    ghost.style.left = target.left + 'px';
    ghost.style.top = target.top + 'px';
    ghost.style.width = target.width + 'px';
    ghost.style.height = target.height + 'px';
    document.body.append(ghost);

    const land = () => {
      ghost.remove();
      item.root.classList.remove('msg--flying');
      item.root.classList.add('msg--landed');
    };

    let from;
    let fade;
    if (origin.kind === 'input') {
      // Текст начинается ровно там, где он был в поле ввода, и «обрастает» пузырём.
      const dx = origin.rect.left - (target.left + 14);
      const dy = origin.rect.top + 9 - (target.top + 10);
      from = 'translate(' + dx + 'px, ' + dy + 'px)';
      fade = [{ opacity: 0 }, { opacity: 1 }];
      ghost.animate({ color: [getComputedStyle(dom.input).color, '#fff'] }, { duration: 260, easing: 'ease-out' });
    } else {
      // С карточки: пузырь вылетает из её центра уменьшенным.
      const scale = 0.55;
      const dx = origin.rect.left + origin.rect.width / 2 - (target.left + (target.width * scale) / 2);
      const dy = origin.rect.top + origin.rect.height / 2 - (target.top + (target.height * scale) / 2);
      from = 'translate(' + dx + 'px, ' + dy + 'px) scale(' + scale + ')';
      fade = [{ opacity: 0.2 }, { opacity: 1 }];
      ghost.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 160, easing: 'ease-out' });
    }

    ghost.firstChild.animate(fade, { duration: 300, easing: 'ease-out', fill: 'both' });
    const flight = ghost.animate(
      [{ transform: from }, { transform: 'none' }],
      { duration: 520, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' }
    );
    flight.onfinish = land;
    flight.oncancel = land;
  }

  async function send(rawText, failedItem, origin, attachment) {
    const text = String(rawText).trim();
    if ((!text && !attachment) || state.busy) return;

    if (state.finishReveal) state.finishReveal();
    clearNotice();
    setBusy(true);
    haptic('impact', 'light');

    const fly = Boolean(origin) && !attachment && !failedItem && !reducedMotion && typeof Element.prototype.animate === 'function';
    let item = failedItem;
    if (item) {
      item.root.classList.remove('msg--failed');
    } else {
      item = addMessage(
        { role: 'user', content: text, ts: nowTs() },
        { fresh: true, flight: fly, image: attachment ? attachment.url : null }
      );
    }
    updateWelcome();
    const typing = showTyping();
    let live = null;
    if (fly) {
      // Сначала лента встаёт на место, потом сообщение летит в уже известную точку.
      scrollToBottom(false);
      flyIn(item, origin);
    } else {
      scrollToBottom(true);
    }

    try {
      let data;
      if (attachment) {
        data = await backend.photo(text, attachment.blob, state.model);
      } else {
        data = await backend.chatStream(text, state.model, (piece) => {
          // Первый кусок ответа — убираем «печатает» и открываем пузырь.
          if (!live) {
            typing.dismiss();
            live = startStreamingReply();
          }
          live.text += piece;
          live.view.push(live.text);
        });
      }
      typing.dismiss();
      if (live) finishStreamingReply(live, data.reply);
      else addAssistantReply(data.reply);
      haptic('impact', 'soft');
      if (data.usage) state.usage = data.usage;
      watchNewMemories();
      if (data.reply && data.reply.provider === 'reminders') refreshReminders();
      state.signature = '';
      state.syncedAt = Date.now();
      setBusy(false);
    } catch (error) {
      typing.dismiss();
      if (live) {
        // Ответ оборвался посреди: показанная часть остаётся, но помечается.
        live.view.flush();
        live.item.root.classList.add('msg--failed');
      }
      setBusy(false);
      haptic('notify', 'error');
      if (error.status === 401 || error.status === 403) {
        showLoadError(error, boot);
        return;
      }
      if (error.code === 'network') setStatus('offline');
      item.root.classList.add('msg--failed');
      showNotice(error.message, () => send(text, item, null, attachment));
      scrollToBottom(true);
    }
  }

  /* ---------- Фото ---------- */

  const MAX_PHOTO_SIDE = 1600;

  /* Уменьшает фото на телефоне (до 1600 px, JPEG): быстрее отправка, и стираются
     служебные данные снимка вроде геолокации. */
  async function preparePhoto(file) {
    const source = URL.createObjectURL(file);
    try {
      const image = new Image();
      image.src = source;
      await image.decode();
      const scale = Math.min(1, MAX_PHOTO_SIDE / Math.max(image.naturalWidth, image.naturalHeight));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      const context = canvas.getContext('2d');
      context.fillStyle = '#fff'; // у PNG с прозрачностью фон станет белым, а не чёрным
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.86));
      if (!blob) throw new Error('encode');
      return blob;
    } finally {
      URL.revokeObjectURL(source);
    }
  }

  async function pickPhoto(file) {
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      showToast('Это не картинка: выбери фото или скриншот.');
      return;
    }
    let blob;
    try {
      blob = await preparePhoto(file);
    } catch (error) {
      showToast('Не получилось открыть картинку. Попробуй другое фото.');
      return;
    }
    clearAttachment(true);
    state.attachment = { blob, url: URL.createObjectURL(blob) };
    dom.attachmentImg.src = state.attachment.url;
    dom.attachment.classList.remove('is-leaving');
    dom.attachment.hidden = false;
    dom.input.placeholder = 'Подпись или вопрос к фото…';
    haptic('impact', 'light');
    updateComposer();
    if (finePointer) dom.input.focus();
  }

  /* keepUrl — ссылка ещё нужна пузырю сообщения, в котором показано фото. */
  function clearAttachment(instant, keepUrl) {
    const current = state.attachment;
    state.attachment = null;
    dom.input.placeholder = 'Напиши сообщение…';
    if (current && !keepUrl) URL.revokeObjectURL(current.url);
    if (dom.attachment.hidden) return updateComposer();
    if (instant || reducedMotion) {
      dom.attachment.hidden = true;
    } else {
      dom.attachment.classList.add('is-leaving');
      setTimeout(() => {
        if (!state.attachment) dom.attachment.hidden = true;
        dom.attachment.classList.remove('is-leaving');
      }, 220);
    }
    updateComposer();
  }

  /* ---------- Голосовой ввод ---------- */

  const MAX_RECORD_SECONDS = 300;

  function formatSeconds(total) {
    const seconds = Math.floor(total);
    return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
  }

  async function startRecording() {
    if (state.busy || state.recording || state.transcribing) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      showToast('Нет доступа к микрофону. Голосовое можно отправить прямо в чат с ботом.');
      haptic('notify', 'error');
      return;
    }
    const types = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm'];
    const mimeType = types.find((type) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(type));
    let recorder;
    try {
      recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    } catch (error) {
      stream.getTracks().forEach((track) => track.stop());
      showToast('Запись голоса не поддерживается в этой версии Telegram.');
      return;
    }
    const rec = { recorder, stream, chunks: [], started: Date.now(), timer: null, cancelled: false };
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size) rec.chunks.push(event.data);
    };
    recorder.onstop = () => finishRecording(rec);
    recorder.start(250);
    state.recording = rec;
    dom.recTime.textContent = '0:00';
    dom.recHint.textContent = '➤ — отправить';
    dom.recorder.hidden = false;
    dom.composerBox.classList.add('is-recording');
    rec.timer = setInterval(() => {
      const seconds = (Date.now() - rec.started) / 1000;
      dom.recTime.textContent = formatSeconds(seconds);
      if (seconds >= MAX_RECORD_SECONDS) stopRecording(false);
    }, 250);
    haptic('impact', 'medium');
    updateComposer();
  }

  function stopRecording(cancel) {
    const rec = state.recording;
    if (!rec) return;
    rec.cancelled = Boolean(cancel);
    clearInterval(rec.timer);
    if (rec.recorder.state !== 'inactive') rec.recorder.stop();
    else finishRecording(rec);
  }

  async function finishRecording(rec) {
    if (rec.finished) return;
    rec.finished = true;
    rec.stream.getTracks().forEach((track) => track.stop());
    state.recording = null;
    dom.composerBox.classList.remove('is-recording');
    const seconds = (Date.now() - rec.started) / 1000;
    if (rec.cancelled || !rec.chunks.length || seconds < 0.7) {
      dom.recorder.hidden = true;
      if (!rec.cancelled) showToast('Слишком коротко: нажми микрофон и говори');
      haptic('impact', 'light');
      updateComposer();
      return;
    }

    const type = (rec.recorder.mimeType || rec.chunks[0].type || 'audio/webm').split(';')[0];
    const blob = new Blob(rec.chunks, { type });
    state.transcribing = true;
    dom.composerBox.classList.add('is-transcribing');
    dom.send.classList.add('is-busy');
    dom.recHint.textContent = 'Распознаю голосовое…';
    updateComposer();
    let text = '';
    let failed = false;
    try {
      const data = await backend.voice(blob, type);
      text = (data && data.text ? String(data.text) : '').trim();
    } catch (error) {
      failed = true;
      showToast(error.message);
      haptic('notify', 'error');
    } finally {
      state.transcribing = false;
      dom.composerBox.classList.remove('is-transcribing');
      dom.send.classList.remove('is-busy');
      dom.recorder.hidden = true;
      updateComposer();
    }
    if (!text) {
      if (!failed) showToast('Не расслышала ни слова. Попробуй ещё раз 🙂');
      return;
    }
    // Распознанный текст «вылетает» из поля ввода, как обычное сообщение.
    send(text, null, { kind: 'input', rect: dom.input.getBoundingClientRect() });
  }

  function submit() {
    if (state.recording) {
      stopRecording(false);
      return;
    }
    const text = dom.input.value;
    if (state.attachment && !state.busy) {
      const attachment = state.attachment;
      dom.input.value = '';
      autosize();
      clearAttachment(false, true);
      send(text, null, null, attachment);
      return;
    }
    if (!text.trim() || state.busy) return;
    const origin = { kind: 'input', rect: dom.input.getBoundingClientRect() };
    dom.input.value = '';
    autosize();
    updateComposer();
    dom.heroOrb.classList.remove('orb--listening');
    dom.avatar.classList.remove('orb--listening');
    send(text, null, origin);
  }

  /* ---------- Копирование ---------- */

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (error) {
      const area = el('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.append(area);
      area.select();
      let ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (fallbackError) { /* копирование запрещено */ }
      area.remove();
      return ok;
    }
  }

  async function onCopyClick(button) {
    if (button.classList.contains('is-done')) return;
    const ok = await copyText(button.dataset.copy);
    if (!ok) {
      showToast('Не получилось скопировать');
      return;
    }
    haptic('select');
    const saved = Array.from(button.childNodes).filter((node) => !(node.classList && node.classList.contains('ripple-wave')));
    button.classList.add('is-done');
    button.replaceChildren(icon('i-check'), el('span', '', 'Скопировано'));
    setTimeout(() => {
      button.classList.remove('is-done');
      button.replaceChildren(...saved);
    }, 1600);
  }

  /* ---------- Всплывающее уведомление ---------- */

  let toastTimer = null;

  function showToast(text) {
    clearTimeout(toastTimer);
    dom.toast.classList.remove('is-leaving');
    dom.toast.hidden = true;
    dom.toast.textContent = text;
    void dom.toast.offsetWidth; // перезапуск анимации
    dom.toast.hidden = false;
    toastTimer = setTimeout(() => {
      dom.toast.classList.add('is-leaving');
      toastTimer = setTimeout(() => {
        dom.toast.hidden = true;
        dom.toast.classList.remove('is-leaving');
      }, reducedMotion ? 0 : 260);
    }, 2400);
  }

  /* ---------- Настройки ---------- */

  function renderModels() {
    const options = [{ id: AUTO, title: 'Авто', sub: 'Эля сама выберет доступную модель' }].concat(
      state.models.map((item) => ({
        id: item.id,
        title: modelTitle(item.model),
        sub: item.provider + (/:free$/.test(item.model) ? ' · бесплатная' : ''),
      }))
    );

    dom.modelList.replaceChildren(
      ...options.map((option) => {
        const button = el('button', 'option');
        button.type = 'button';
        button.setAttribute('role', 'radio');
        button.setAttribute('aria-checked', String(option.id === state.model));
        button.dataset.model = option.id;
        const text = el('span', 'option__text');
        text.append(el('span', 'option__title', option.title), el('span', 'option__sub', option.sub));
        const mark = el('span', 'option__mark');
        mark.append(icon('i-check'));
        button.append(text, mark);
        return button;
      })
    );
  }

  function setSegment(control, value) {
    const items = Array.from(control.querySelectorAll('[data-value]'));
    const index = Math.max(0, items.findIndex((item) => item.dataset.value === value));
    control.style.setProperty('--count', String(items.length));
    control.style.setProperty('--index', String(index));
    items.forEach((item, i) => item.setAttribute('aria-checked', String(i === index)));
  }

  function renderPreferences() {
    const prefs = state.preferences;
    const options = state.preferenceOptions;

    dom.styleList.replaceChildren(...options.style.map((option) => {
      const button = el('button', 'option');
      button.type = 'button';
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-checked', String(option.id === prefs.style));
      button.dataset.style = option.id;
      const emoji = el('span', 'option__emoji', STYLE_EMOJI[option.id] || '✨');
      emoji.setAttribute('aria-hidden', 'true');
      const text = el('span', 'option__text');
      text.append(el('span', 'option__title', option.title), el('span', 'option__sub', option.description));
      const mark = el('span', 'option__mark');
      mark.append(icon('i-check'));
      button.append(emoji, text, mark);
      return button;
    }));

    // Варианты полноты ответа приходят с сервера; подложку-«таблетку» не трогаем.
    if (!dom.verbosityControl.querySelector('[data-value]')) {
      options.verbosity.forEach((option) => {
        const button = el('button', 'segmented__item');
        button.type = 'button';
        button.setAttribute('role', 'radio');
        button.dataset.value = option.id;
        button.append(el('span', 'segmented__title', option.title));
        dom.verbosityControl.append(button);
      });
    }
    setSegment(dom.verbosityControl, prefs.verbosity);
    const current = options.verbosity.find((option) => option.id === prefs.verbosity);
    dom.verbosityNote.textContent = current ? current.description + '.' : '';
    setSegment(dom.themeControl, state.theme);
  }

  /* ---------- Инструкции ---------- */

  function renderInstructionsSummary() {
    const data = state.instructions;
    dom.instructionsBlock.hidden = !data;
    if (!data) return;
    const parts = [];
    if (data.about) parts.push('о тебе');
    if (data.style) parts.push('как отвечать');
    const filled = parts.length > 0;
    dom.instructionsSummary.textContent = !filled
      ? 'Не заданы'
      : (data.enabled ? 'Включены' : 'Выключены') + ' · ' + parts.join(', ');
    dom.instructionsSummary.classList.toggle('is-on', filled && data.enabled);
  }

  function instructionsDirty() {
    const data = state.instructions;
    return Boolean(data) && state.instructionsEdited &&
      (dom.instrAbout.value.trim() !== data.about || dom.instrStyle.value.trim() !== data.style);
  }

  function updateInstructionsForm() {
    const max = (state.instructions && state.instructions.max_length) || 1500;
    [[dom.instrAbout, dom.instrAboutCount], [dom.instrStyle, dom.instrStyleCount]].forEach(([field, count]) => {
      field.maxLength = max;
      count.textContent = field.value.length + ' / ' + max;
      count.classList.toggle('is-near', field.value.length > max * 0.9);
    });
    dom.instrSave.disabled = !instructionsDirty();
    const enabled = Boolean(state.instructions && state.instructions.enabled);
    dom.instructionsToggle.setAttribute('aria-checked', String(enabled));
    dom.instructionsStatus.textContent = enabled
      ? 'В новых ответах — здесь и в чате с ботом'
      : 'Выключены: Эля их сейчас не учитывает';
  }

  /* Переход между страницами панели: основная ↔ инструкции. */
  function switchSheetPage(page) {
    if (state.sheetPage === page) return;
    const forward = page === 'instructions';
    const from = forward ? dom.sheetBody : dom.instructionsPage;
    const to = forward ? dom.instructionsPage : dom.sheetBody;
    state.sheetPage = page;
    dom.sheet.classList.toggle('is-deep', forward);
    dom.sheetBack.hidden = !forward;
    dom.sheetTitle.textContent = forward ? 'Инструкции' : 'Настройки';
    motion(dom.sheetTitle, [{ opacity: 0, transform: 'translateX(' + (forward ? 12 : -12) + 'px)' }, { opacity: 1, transform: 'none' }],
      { duration: 280, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
    from.hidden = true;
    to.hidden = false;
    to.scrollTop = 0;
    motion(to, [
      { opacity: 0, transform: 'translateX(' + (forward ? 36 : -36) + 'px)' },
      { opacity: 1, transform: 'none' },
    ], { duration: 360, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' });
  }

  function openInstructionsPage() {
    if (!state.instructions) return;
    if (!instructionsDirty()) {
      // Несохранённых правок нет — показываем то, что сохранено на сервере.
      state.instructionsEdited = false;
      dom.instrAbout.value = state.instructions.about;
      dom.instrStyle.value = state.instructions.style;
    }
    updateInstructionsForm();
    switchSheetPage('instructions');
    haptic('impact', 'light');
  }

  async function saveInstructions(quiet) {
    if (!instructionsDirty()) return true;
    dom.instrSave.disabled = true;
    try {
      state.instructions = await backend.saveInstructions({
        about: dom.instrAbout.value,
        style: dom.instrStyle.value,
      });
      // Сервер чистит пробелы — показываем ровно то, что сохранено.
      dom.instrAbout.value = state.instructions.about;
      dom.instrStyle.value = state.instructions.style;
      state.instructionsEdited = false;
      renderInstructionsSummary();
      updateInstructionsForm();
      showToast(quiet ? 'Инструкции сохранены' : '✍️ Сохранено — Эля учтёт это в новых ответах');
      haptic('notify', 'success');
      return true;
    } catch (error) {
      updateInstructionsForm();
      showToast(error.message);
      haptic('notify', 'error');
      return false;
    }
  }

  async function toggleInstructions() {
    const data = state.instructions;
    if (!data) return;
    const next = !data.enabled;
    data.enabled = next;
    updateInstructionsForm();
    renderInstructionsSummary();
    haptic('select');
    try {
      const saved = await backend.saveInstructions({ enabled: next });
      data.enabled = saved.enabled;
    } catch (error) {
      data.enabled = !next;
      showToast(error.message);
    }
    updateInstructionsForm();
    renderInstructionsSummary();
  }

  /* «Назад» из инструкций сохраняет изменения сам — как в настройках телефона. */
  async function leaveInstructionsPage() {
    if (state.sheetPage !== 'instructions') return;
    if (instructionsDirty() && !(await saveInstructions(true))) return;
    switchSheetPage('main');
  }

  function insertChip(chip) {
    const field = $(chip.parentElement.dataset.target);
    const text = chip.dataset.insert;
    field.value = field.value.trim() ? field.value.replace(/\s+$/, '') + '\n' + text : text;
    field.focus();
    state.instructionsEdited = true;
    // Курсор — на место многоточия, если оно есть, иначе в конец.
    const gap = field.value.lastIndexOf('…');
    const position = gap > field.value.length - text.length - 1 ? gap : field.value.length;
    field.setSelectionRange(position, gap === position ? position + 1 : position);
    updateInstructionsForm();
    haptic('select');
  }

  /* ---------- Напоминания ---------- */

  const REPEAT_TITLES = { daily: 'каждый день', weekdays: 'по будням', weekly: 'каждую неделю', monthly: 'каждый месяц' };
  const dueDayFormat = new Intl.DateTimeFormat('ru-RU', { weekday: 'short', day: 'numeric', month: 'long' });

  function formatDue(ts) {
    const date = new Date(ts * 1000);
    const clock = timeFormat.format(date);
    const today = new Date();
    const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
    if (dayKey(date) === dayKey(today)) return 'Сегодня, ' + clock;
    if (dayKey(date) === dayKey(tomorrow)) return 'Завтра, ' + clock;
    return dueDayFormat.format(date) + ', ' + clock;
  }

  function deviceTimezone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    } catch (error) {
      return '';
    }
  }

  /* Значение для <input type="datetime-local">: местное время «ГГГГ-ММ-ДДTЧЧ:ММ». */
  function localInputValue(date) {
    const pad = (n) => String(n).padStart(2, '0');
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) +
      'T' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function suggestedReminderTime() {
    const date = new Date(Date.now() + 3600 * 1000);
    date.setMinutes(Math.ceil(date.getMinutes() / 5) * 5, 0, 0);
    return date;
  }

  function updateBell(previous) {
    const count = state.reminders.items.length;
    dom.bellBadge.hidden = count === 0;
    dom.bellBadge.textContent = count > 99 ? '99+' : String(count);
    if (typeof previous === 'number' && count > previous) {
      dom.bellBadge.classList.remove('is-bump');
      void dom.bellBadge.offsetWidth;
      dom.bellBadge.classList.add('is-bump');
      dom.bellBtn.classList.remove('is-ringing');
      void dom.bellBtn.offsetWidth;
      dom.bellBtn.classList.add('is-ringing');
    }
  }

  function renderReminders(options) {
    const opts = options || {};
    const { items, timezone } = state.reminders;
    const soon = Date.now() / 1000 + 3600;
    dom.remindersList.replaceChildren(...items.map((item, index) => {
      const fresh = opts.fresh && opts.fresh.has(item.id);
      const li = el('li', 'fact' + (fresh ? ' fact--fresh' : opts.stagger ? ' fact--in' : ''));
      li.style.setProperty('--i', index);
      li.dataset.id = String(item.id);
      const body = el('div', 'fact__body');
      body.append(el('span', 'fact__text', item.text));
      const meta = el('div', 'fact__meta');
      meta.append(el('b', '', formatDue(item.due)));
      if (REPEAT_TITLES[item.repeat]) meta.append(el('span', 'fact__tag', '🔁 ' + REPEAT_TITLES[item.repeat]));
      body.append(meta);
      const remove = el('button', 'fact__remove ripple');
      remove.type = 'button';
      remove.dataset.removeReminder = String(item.id);
      remove.setAttribute('aria-label', 'Удалить напоминание: ' + item.text);
      remove.append(icon('i-close'));
      li.append(body, remove);
      if (item.due < soon) li.classList.add('reminders-soon');
      return li;
    }));
    dom.remindersEmpty.hidden = items.length > 0;
    dom.reminderText.maxLength = state.reminders.max_text;
    dom.reminderAdd.disabled = !dom.reminderText.value.trim() || !dom.reminderWhen.value;
    dom.tzNote.textContent = 'Эля пришлёт напоминание сообщением в Telegram. Часовой пояс' +
      (timezone ? ': ' + timezone + ' (определён по телефону).' : ' определяется по телефону.');
    updateBell();
  }

  function takeReminders(data, options) {
    const previous = state.reminders.items.length;
    state.reminders = Object.assign({}, state.reminders, data);
    renderReminders(options);
    updateBell(previous);
  }

  /* Часовой пояс телефона — единственный источник истины: от него зависит время напоминаний. */
  async function syncTimezone() {
    const device = deviceTimezone();
    if (!device || device === state.reminders.timezone) return;
    try {
      takeReminders(await backend.setTimezone(device));
    } catch (error) { /* напоминания из чата попросят пояс сами */ }
  }

  async function addReminder() {
    const text = dom.reminderText.value.trim();
    const when = dom.reminderWhen.value;
    if (!text || !when) return;
    dom.reminderAdd.disabled = true;
    try {
      const before = new Set(state.reminders.items.map((item) => item.id));
      const data = await backend.reminderAdd(text, when, dom.reminderRepeat.value);
      const fresh = new Set(data.items.filter((item) => !before.has(item.id)).map((item) => item.id));
      dom.reminderText.value = '';
      dom.reminderRepeat.value = 'none';
      dom.reminderWhen.value = localInputValue(suggestedReminderTime());
      takeReminders(data, { fresh });
      haptic('notify', 'success');
      showToast('⏰ Напомню: ' + formatDue(data.items.find((item) => fresh.has(item.id)).due).toLowerCase());
    } catch (error) {
      renderReminders();
      showToast(error.message);
      haptic('notify', 'error');
    }
  }

  async function removeReminder(id) {
    const item = dom.remindersList.querySelector('[data-id="' + id + '"]');
    if (item) item.classList.add('is-leaving');
    haptic('impact', 'light');
    try {
      const [data] = await Promise.all([backend.reminderDelete(id), sleep(reducedMotion ? 0 : 300)]);
      takeReminders(data);
    } catch (error) {
      if (item) item.classList.remove('is-leaving');
      showToast(error.message);
      haptic('notify', 'error');
    }
  }

  /* Напоминание могли поставить фразой в чате — подтягиваем список с сервера. */
  async function refreshReminders() {
    try {
      const data = await backend.state();
      if (data.reminders) takeReminders(data.reminders);
    } catch (error) { /* не критично */ }
  }

  /* ---------- Память ---------- */

  function memoryText(count) {
    const max = state.memory.max_facts;
    return state.memory.enabled
      ? (count ? 'Помню ' + count + ' из ' + max : 'Включено, пока ничего не помню')
      : 'Выключено' + (count ? ': ' + count + ' сохранено, не использую' : '');
  }

  /* options.fresh — id только что добавленных фактов (мигают), options.stagger — первая отрисовка. */
  function renderMemory(options) {
    const opts = options || {};
    const memory = state.memory;
    const facts = memory.facts;
    dom.memory.classList.toggle('is-on', memory.enabled);
    dom.memoryToggle.setAttribute('aria-checked', String(memory.enabled));
    dom.memorySub.textContent = memoryText(facts.length);

    dom.facts.replaceChildren(...facts.map((fact, index) => {
      const fresh = opts.fresh && opts.fresh.has(fact.id);
      const li = el('li', 'fact' + (fresh ? ' fact--fresh' : opts.stagger ? ' fact--in' : ''));
      li.style.setProperty('--i', index);
      li.dataset.id = String(fact.id);
      const remove = el('button', 'fact__remove ripple');
      remove.type = 'button';
      remove.dataset.remove = String(fact.id);
      remove.setAttribute('aria-label', 'Забыть: ' + fact.text);
      remove.append(icon('i-close'));
      li.append(el('span', 'fact__text', fact.text), remove);
      return li;
    }));

    dom.factsEmpty.hidden = facts.length > 0;
    dom.factsEmpty.textContent = memory.enabled
      ? 'Пока ничего не запомнила. Расскажи о себе в разговоре или добавь факт ниже.'
      : 'Включи память, и я начну запоминать важное. Можно и самому добавить факт ниже.';
    dom.factsClear.hidden = facts.length === 0;
    dom.factInput.maxLength = memory.max_fact_length;
    dom.factAdd.disabled = !dom.factInput.value.trim();
    disarmClear();
  }

  function disarmClear() {
    clearTimeout(state.clearArmed);
    state.clearArmed = null;
    dom.factsClear.classList.remove('is-armed');
    dom.factsClearLabel.textContent = 'Забыть всё';
  }

  /* Ответ сервера с обновлённой памятью. */
  function takeMemory(data, options) {
    state.memory = Object.assign({}, state.memory, {
      enabled: data.enabled,
      facts: data.facts || [],
      max_facts: data.max_facts || state.memory.max_facts,
      max_fact_length: data.max_fact_length || state.memory.max_fact_length,
    });
    renderMemory(options);
  }

  async function toggleMemory() {
    const next = !state.memory.enabled;
    state.memory.enabled = next;
    renderMemory();
    haptic('select');
    try {
      takeMemory(await backend.memorySet(next));
    } catch (error) {
      state.memory.enabled = !next;
      renderMemory();
      showToast(error.message);
      haptic('notify', 'error');
    }
  }

  async function addFact() {
    const text = dom.factInput.value.trim();
    if (!text) return;
    dom.factAdd.disabled = true;
    try {
      const data = await backend.memoryAdd(text);
      dom.factInput.value = '';
      if (data.added) {
        takeMemory(data, { fresh: new Set([data.added.id]) });
        haptic('notify', 'success');
      } else {
        takeMemory(data);
        showToast('Это я уже помню');
      }
    } catch (error) {
      renderMemory();
      showToast(error.message);
      haptic('notify', 'error');
    }
  }

  async function removeFact(id) {
    const item = dom.facts.querySelector('[data-id="' + id + '"]');
    if (item) item.classList.add('is-leaving');
    haptic('impact', 'light');
    try {
      const [data] = await Promise.all([backend.memoryDelete(id), sleep(reducedMotion ? 0 : 300)]);
      takeMemory(data);
    } catch (error) {
      if (item) item.classList.remove('is-leaving');
      showToast(error.message);
      haptic('notify', 'error');
    }
  }

  async function clearMemory() {
    // Первое нажатие «взводит» кнопку, второе в течение 3 секунд подтверждает.
    if (!state.clearArmed) {
      dom.factsClear.classList.add('is-armed');
      dom.factsClearLabel.textContent = 'Нажми ещё раз — забуду всё';
      haptic('notify', 'warning');
      state.clearArmed = setTimeout(disarmClear, 3000);
      return;
    }
    disarmClear();
    dom.facts.querySelectorAll('.fact').forEach((li, i) => {
      li.style.setProperty('--i', i);
      li.classList.add('is-leaving');
    });
    try {
      const [data] = await Promise.all([backend.memoryClear(), sleep(reducedMotion ? 0 : 340)]);
      takeMemory(data);
      showToast('Всё забыла');
      haptic('notify', 'success');
    } catch (error) {
      renderMemory();
      showToast(error.message);
    }
  }

  /* После ответа Эля могла что-то запомнить (это идёт в фоне), поэтому чуть позже
     заглядываем в память и, если там появилось новое, говорим об этом. */
  function watchNewMemories() {
    state.memoryTimers.forEach(clearTimeout);
    state.memoryTimers = [];
    if (!state.memory.enabled) return;
    [6000, 15000].forEach((delay) => {
      state.memoryTimers.push(setTimeout(async () => {
        if (!state.memory.enabled) return;
        let data;
        try {
          data = await backend.state();
        } catch (error) {
          return;
        }
        if (!data.memory) return;
        const known = new Set(state.memory.facts.map((fact) => fact.id));
        const added = data.memory.facts.filter((fact) => !known.has(fact.id));
        if (!added.length) return;
        state.memoryTimers.forEach(clearTimeout);
        takeMemory(data.memory, { fresh: new Set(added.map((fact) => fact.id)) });
        const first = added[0].text.length > 60 ? added[0].text.slice(0, 57) + '…' : added[0].text;
        showToast('🧠 Запомнила: ' + first + (added.length > 1 ? ' (+' + (added.length - 1) + ')' : ''));
      }, delay));
    });
  }

  async function savePreferences(patch) {
    const previous = state.preferences;
    state.preferences = Object.assign({}, previous, patch);
    renderPreferences();
    haptic('select');
    try {
      const data = await backend.savePreferences(patch);
      if (data && data.preferences) state.preferences = data.preferences;
    } catch (error) {
      state.preferences = previous;
      renderPreferences();
      showToast(error.message);
      haptic('notify', 'error');
    }
  }

  function renderUsage() {
    const used = state.usage.used;
    const limit = state.usage.limit;
    countUp(dom.usageValue, used);
    dom.usageLimit.textContent = limit ? 'из ' + limit : 'без дневного лимита';
    dom.usageBar.hidden = !limit;
    dom.usageFill.style.width = limit ? Math.min(100, Math.round((used / limit) * 100)) + '%' : '0';
  }

  function resetClearConfirm() {
    dom.clearBtn.hidden = false;
    dom.clearConfirm.hidden = true;
    dom.clearYes.disabled = false;
  }

  function openSheet(options) {
    const opts = options || {};
    renderModels();
    renderPreferences();
    renderMemory({ stagger: true });
    dom.reminderWhen.min = localInputValue(new Date());
    if (!dom.reminderWhen.value) dom.reminderWhen.value = localInputValue(suggestedReminderTime());
    renderReminders({ stagger: true });
    resetClearConfirm();
    if (state.sheetAnim) state.sheetAnim.cancel();
    state.sheetClosing = false;
    dom.sheet.getAnimations().forEach((animation) => animation.cancel());
    dom.backdrop.getAnimations().forEach((animation) => animation.cancel());
    dom.sheet.style.transform = '';
    dom.backdrop.style.opacity = '';
    dom.backdrop.hidden = false;
    dom.sheet.hidden = false;
    dom.sheet.classList.remove('is-opening');
    void dom.sheet.offsetWidth;
    dom.sheet.classList.add('is-opening');
    renderInstructionsSummary();
    dom.input.blur();

    state.sheetAnim = motion(dom.sheet, isWide()
      ? [{ opacity: 0, transform: 'translateY(18px) scale(0.95)' }, { opacity: 1, transform: 'none' }]
      : [{ transform: 'translateY(100%)' }, { transform: 'none' }],
    { duration: isWide() ? 420 : 560, easing: 'cubic-bezier(0.2, 1.08, 0.32, 1)' });
    motion(dom.backdrop, [{ opacity: 0 }, { opacity: 1 }], { duration: 300, easing: 'ease-out' });

    // Полоска и число заполняются уже на глазах.
    dom.usageFill.style.width = '0';
    dom.usageValue.textContent = '0';
    setTimeout(renderUsage, reducedMotion ? 0 : 180);
    if (supports('6.1')) tg.BackButton.show();
    haptic('impact', 'light');
    if (opts.focus === 'reminders') {
      // Колокольчик открывает настройки сразу на напоминаниях.
      setTimeout(() => {
        // offsetTop не зависит от анимации появления блоков (transform), в отличие от getBoundingClientRect.
        dom.sheetBody.scrollTo({ top: dom.remindersBlock.offsetTop - 8, behavior: reducedMotion ? 'auto' : 'smooth' });
      }, reducedMotion ? 0 : 380);
    } else {
      dom.sheetBody.scrollTop = 0;
    }
  }

  function closeSheet() {
    if (dom.sheet.hidden || state.sheetClosing) return;
    if (state.sheetPage === 'instructions' && instructionsDirty()) saveInstructions(true);
    state.sheetClosing = true;
    if (supports('6.1')) tg.BackButton.hide();

    const fromTransform = dom.sheet.style.transform || 'none';
    const fromOpacity = dom.backdrop.style.opacity || '1';
    if (state.sheetAnim) state.sheetAnim.cancel();

    const sheetOut = motion(dom.sheet, isWide()
      ? [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(14px) scale(0.96)' }]
      : [{ transform: fromTransform }, { transform: 'translateY(100%)' }],
    { duration: 260, easing: 'cubic-bezier(0.4, 0, 0.9, 0.6)', fill: 'forwards' });
    const backdropOut = motion(dom.backdrop, [{ opacity: fromOpacity }, { opacity: 0 }], { duration: 260, fill: 'forwards' });

    const done = () => {
      dom.sheet.hidden = true;
      dom.backdrop.hidden = true;
      if (sheetOut) sheetOut.cancel();
      if (backdropOut) backdropOut.cancel();
      dom.sheet.style.transform = '';
      dom.backdrop.style.opacity = '';
      dom.sheet.classList.remove('is-opening', 'is-dragging', 'is-deep');
      // В следующий раз панель открывается с основной страницы.
      state.sheetPage = 'main';
      dom.instructionsPage.hidden = true;
      dom.sheetBody.hidden = false;
      dom.sheetBack.hidden = true;
      dom.sheetTitle.textContent = 'Настройки';
      state.sheetClosing = false;
    };
    if (sheetOut) sheetOut.onfinish = done;
    else done();
  }

  /* Панель можно смахнуть вниз за «ручку» или заголовок. */
  function bindSheetDrag() {
    let pointer = null;
    let startY = 0;
    let lastY = 0;
    let lastTime = 0;
    let velocity = 0;
    let height = 0;

    function down(event) {
      if (isWide() || event.button > 0 || event.target.closest('button') || state.sheetClosing) return;
      pointer = event.pointerId;
      startY = lastY = event.clientY;
      lastTime = event.timeStamp;
      velocity = 0;
      height = dom.sheet.offsetHeight;
      if (state.sheetAnim) {
        state.sheetAnim.cancel();
        state.sheetAnim = null;
      }
      event.currentTarget.setPointerCapture(pointer);
      dom.sheet.classList.add('is-dragging');
    }

    function move(event) {
      if (event.pointerId !== pointer) return;
      const dy = event.clientY - startY;
      const offset = dy > 0 ? dy : dy / 5; // вверх — с сопротивлением
      dom.sheet.style.transform = 'translateY(' + offset + 'px)';
      dom.backdrop.style.opacity = String(Math.max(0, 1 - Math.max(0, offset) / height));
      const dt = event.timeStamp - lastTime;
      if (dt > 0) velocity = (event.clientY - lastY) / dt;
      lastY = event.clientY;
      lastTime = event.timeStamp;
    }

    function up(event) {
      if (event.pointerId !== pointer) return;
      pointer = null;
      dom.sheet.classList.remove('is-dragging');
      const dy = event.clientY - startY;
      if (dy > Math.min(140, height * 0.3) || (dy > 10 && velocity > 0.5)) {
        haptic('impact', 'light');
        closeSheet();
        return;
      }
      // Не дотянули — панель пружинит обратно.
      const fromTransform = dom.sheet.style.transform || 'none';
      const fromOpacity = dom.backdrop.style.opacity || '1';
      dom.sheet.style.transform = '';
      dom.backdrop.style.opacity = '';
      motion(dom.sheet, [{ transform: fromTransform }, { transform: 'none' }], { duration: 480, easing: SPRING });
      motion(dom.backdrop, [{ opacity: fromOpacity }, { opacity: 1 }], { duration: 300, easing: 'ease-out' });
    }

    [dom.sheetGrip, dom.sheetHead].forEach((handle) => {
      handle.addEventListener('pointerdown', down);
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
    });
  }

  async function clearHistory() {
    dom.clearYes.disabled = true;
    try {
      await backend.clear();
    } catch (error) {
      dom.clearYes.disabled = false;
      showToast(error.message);
      haptic('notify', 'error');
      return;
    }
    closeSheet();
    const nodes = Array.from(dom.messages.children).slice(-14).reverse();
    nodes.forEach((node, index) => {
      node.style.setProperty('--i', index);
      node.classList.add('is-leaving');
    });
    if (nodes.length && !reducedMotion) await sleep(380 + nodes.length * 35);
    renderHistory([]);
    showToast('История очищена');
    haptic('notify', 'success');
  }

  /* ---------- Загрузка и синхронизация ---------- */

  function applyState(data) {
    state.assistantName = (data.assistant && data.assistant.name) || 'Эля';
    state.firstName = (data.user && data.user.first_name) || '';
    state.models = data.models || [];
    state.providers = data.providers || [];
    state.usage = data.usage || state.usage;
    state.maxLength = (data.limits && data.limits.max_text_length) || 4000;
    state.voice = Boolean(data.voice);
    state.vision = Boolean(data.vision);
    if (data.preferences) state.preferences = data.preferences;
    if (data.memory) state.memory = Object.assign({}, state.memory, data.memory);
    if (data.reminders) state.reminders = Object.assign({}, state.reminders, data.reminders);
    state.instructions = data.instructions || null;
    renderInstructionsSummary();
    if (data.preference_options) state.preferenceOptions = data.preference_options;
    state.syncedAt = Date.now();

    const saved = store(MODEL_KEY);
    state.model = state.models.some((item) => item.id === saved) ? saved : AUTO;

    const name = state.assistantName;
    document.title = name;
    dom.assistantName.textContent = name;
    dom.welcomeName.textContent = name;
    dom.input.maxLength = state.maxLength;
    dom.hint.textContent = name + ' — ИИ и может ошибаться. Важное лучше перепроверить.';
    document.querySelectorAll('.orb__letter').forEach((node) => {
      node.textContent = name.charAt(0).toUpperCase();
    });
    dom.privacy.textContent =
      'Сообщения и недавняя история диалога отправляются для ответа во внешние ИИ-сервисы' +
      (state.providers.length ? ': ' + state.providers.join(', ') : '') +
      '. На бесплатных тарифах провайдеры могут использовать данные для улучшения моделей, ' +
      'поэтому не отправляй пароли, номера карт и другие секреты.';
  }

  async function boot() {
    showGate(state.assistantName + ' просыпается…');
    let data;
    try {
      data = await backend.state();
    } catch (error) {
      showLoadError(error, boot);
      return;
    }
    applyState(data);
    updateBell();
    syncTimezone();
    dom.app.hidden = false;
    if (!reducedMotion) {
      dom.app.classList.add('is-entering');
      setTimeout(() => dom.app.classList.remove('is-entering'), 1100);
    }
    setStatus('online');
    renderHistory(data.history || []);
    updateComposer();
    hideGate();
    if (query.get('open') === 'instructions' && state.instructions) {
      setTimeout(() => {
        openSheet();
        setTimeout(openInstructionsPage, reducedMotion ? 0 : 320);
      }, reducedMotion ? 0 : 500);
    } else if (finePointer) dom.input.focus();
  }

  /* Сообщения, отправленные тем временем в чате с ботом, подтягиваются при возврате. */
  async function sync() {
    if (dom.app.hidden || state.busy || state.finishReveal || Date.now() - state.syncedAt < 5000) return;
    state.syncedAt = Date.now();
    let data;
    try {
      data = await backend.state();
    } catch (error) {
      return;
    }
    if (state.busy || state.finishReveal) return;
    state.usage = data.usage || state.usage;
    if (data.memory && dom.sheet.hidden) state.memory = Object.assign({}, state.memory, data.memory);
    if (data.reminders && dom.sheet.hidden) {
      state.reminders = Object.assign({}, state.reminders, data.reminders);
      updateBell();
    }
    setStatus('online');
    const history = data.history || [];
    const signature = historySignature(history);
    if (signature !== state.signature && !state.notice) {
      if (state.signature === '' && history.length === dom.messages.querySelectorAll('.msg').length) {
        state.signature = signature; // лента уже показывает то же самое
      } else {
        renderHistory(history);
      }
    }
  }

  /* ---------- События ---------- */

  function bindEvents() {
    dom.input.addEventListener('input', () => {
      autosize();
      updateComposer();
      // Сфера «прислушивается», пока пользователь печатает.
      const typing = dom.input.value.trim().length > 0;
      dom.heroOrb.classList.toggle('orb--listening', typing);
      dom.avatar.classList.toggle('orb--listening', typing && !state.busy);
    });
    dom.input.addEventListener('keydown', (event) => {
      // На компьютере Enter отправляет, на телефоне — перенос строки.
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && finePointer) {
        event.preventDefault();
        submit();
      }
    });

    // Нажатие на кнопку не должно забирать фокус и прятать клавиатуру.
    dom.send.addEventListener('mousedown', (event) => event.preventDefault());
    dom.send.addEventListener('click', submit);
    dom.mic.addEventListener('click', startRecording);
    dom.attach.addEventListener('click', () => dom.fileInput.click());
    dom.fileInput.addEventListener('change', () => {
      const file = dom.fileInput.files && dom.fileInput.files[0];
      dom.fileInput.value = ''; // чтобы то же фото можно было выбрать ещё раз
      pickPhoto(file);
    });
    dom.attachmentRemove.addEventListener('click', () => clearAttachment(false));
    dom.recCancel.addEventListener('click', () => stopRecording(true));

    dom.suggestions.addEventListener('click', (event) => {
      const card = event.target.closest('.suggestion');
      if (card) send(card.dataset.prompt, null, { kind: 'card', rect: card.getBoundingClientRect() });
    });

    dom.messages.addEventListener('click', (event) => {
      const copy = event.target.closest('[data-copy]');
      if (copy) {
        onCopyClick(copy);
        return;
      }
      const anchor = event.target.closest('a[href]');
      if (anchor && supports('6.1')) {
        event.preventDefault();
        tg.openLink(anchor.href);
      }
    });

    dom.chat.addEventListener('scroll', onScroll, { passive: true });
    dom.chat.addEventListener('touchstart', () => { state.touching = true; }, { passive: true });
    dom.chat.addEventListener('touchend', () => { state.touching = false; }, { passive: true });
    dom.chat.addEventListener('touchcancel', () => { state.touching = false; }, { passive: true });
    dom.chat.addEventListener('wheel', () => { state.wheelAt = Date.now(); }, { passive: true });

    // Волна от касания на кнопках.
    document.addEventListener('pointerdown', (event) => {
      if (reducedMotion) return;
      const target = event.target.closest('.ripple, .option, .chip-btn, .notice__retry, .continue-btn');
      if (!target || target.disabled) return;
      const rect = target.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height) * 2.2;
      const wave = el('span', 'ripple-wave');
      wave.style.width = wave.style.height = size + 'px';
      wave.style.left = event.clientX - rect.left - size / 2 + 'px';
      wave.style.top = event.clientY - rect.top - size / 2 + 'px';
      target.append(wave);
      wave.addEventListener('animationend', () => wave.remove());
    }, { passive: true });
    bindSheetDrag();
    dom.scrollDown.addEventListener('click', () => scrollToBottom(true));

    dom.menuBtn.addEventListener('click', () => openSheet());
    dom.bellBtn.addEventListener('click', () => openSheet({ focus: 'reminders' }));
    dom.reminderForm.addEventListener('submit', (event) => {
      event.preventDefault();
      addReminder();
    });
    const updateReminderAdd = () => {
      dom.reminderAdd.disabled = !dom.reminderText.value.trim() || !dom.reminderWhen.value;
    };
    dom.reminderText.addEventListener('input', updateReminderAdd);
    dom.reminderWhen.addEventListener('input', updateReminderAdd);
    dom.remindersList.addEventListener('click', (event) => {
      const button = event.target.closest('[data-remove-reminder]');
      if (button) removeReminder(Number(button.dataset.removeReminder));
    });
    dom.sheetClose.addEventListener('click', closeSheet);
    dom.sheetBack.addEventListener('click', leaveInstructionsPage);
    dom.openInstructions.addEventListener('click', openInstructionsPage);
    dom.instructionsToggle.addEventListener('click', toggleInstructions);
    dom.instrSave.addEventListener('click', () => saveInstructions(false));
    [dom.instrAbout, dom.instrStyle].forEach((field) => field.addEventListener('input', () => {
      state.instructionsEdited = true;
      updateInstructionsForm();
    }));
    dom.instructionsPage.addEventListener('click', (event) => {
      const chip = event.target.closest('.chip[data-insert]');
      if (chip) insertChip(chip);
    });
    dom.backdrop.addEventListener('click', closeSheet);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        if (state.sheetPage === 'instructions') leaveInstructionsPage();
        else closeSheet();
      }
    });

    dom.memoryToggle.addEventListener('click', toggleMemory);
    dom.factForm.addEventListener('submit', (event) => {
      event.preventDefault();
      addFact();
    });
    dom.factInput.addEventListener('input', () => {
      dom.factAdd.disabled = !dom.factInput.value.trim();
    });
    dom.facts.addEventListener('click', (event) => {
      const button = event.target.closest('[data-remove]');
      if (button) removeFact(Number(button.dataset.remove));
    });
    dom.factsClear.addEventListener('click', clearMemory);

    dom.themeControl.addEventListener('click', (event) => {
      const item = event.target.closest('[data-value]');
      if (!item || item.dataset.value === state.theme) return;
      state.theme = item.dataset.value;
      store(THEME_KEY, state.theme);
      setSegment(dom.themeControl, state.theme);
      haptic('select');
      const rect = item.getBoundingClientRect();
      // Новая тема расходится кругом от нажатой кнопки.
      setTimeout(() => applyTheme({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }), 120);
    });

    dom.verbosityControl.addEventListener('click', (event) => {
      const item = event.target.closest('[data-value]');
      if (item && item.dataset.value !== state.preferences.verbosity) savePreferences({ verbosity: item.dataset.value });
    });

    dom.styleList.addEventListener('click', (event) => {
      const option = event.target.closest('.option');
      if (option && option.dataset.style !== state.preferences.style) savePreferences({ style: option.dataset.style });
    });

    dom.modelList.addEventListener('click', (event) => {
      const option = event.target.closest('.option');
      if (!option || option.dataset.model === state.model) return;
      state.model = option.dataset.model;
      store(MODEL_KEY, state.model);
      renderModels();
      haptic('select');
    });

    dom.clearBtn.addEventListener('click', () => {
      dom.clearBtn.hidden = true;
      dom.clearConfirm.hidden = false;
    });
    dom.clearCancel.addEventListener('click', resetClearConfirm);
    dom.clearYes.addEventListener('click', clearHistory);

    dom.gateBtn.addEventListener('click', () => {
      if (state.gateAction) state.gateAction();
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') sync();
    });
    window.addEventListener('online', () => {
      if (!state.busy) setStatus('online');
    });
    window.addEventListener('offline', () => setStatus('offline'));

    if (window.ResizeObserver) {
      // Клавиатура или выросшее поле ввода меняют высоту ленты — держим её у последнего сообщения.
      new ResizeObserver(() => {
        if (state.stick) scrollToBottom(false);
      }).observe(dom.chat);
      new ResizeObserver(() => {
        dom.app.style.setProperty('--composer-height', dom.composer.offsetHeight + 'px');
      }).observe(dom.composer);
    }
  }

  /* Каркас приложения всегда занимает ровно видимую область: при открытой клавиатуре
     и после её закрытия поле ввода остаётся над клавиатурой, а не уезжает вниз. */
  let viewportFrame = 0;

  function syncViewport() {
    cancelAnimationFrame(viewportFrame);
    viewportFrame = requestAnimationFrame(() => {
      const vv = window.visualViewport;
      const height = vv ? vv.height : window.innerHeight;
      const top = vv ? Math.max(0, vv.offsetTop) : 0;
      const rootStyle = document.documentElement.style;
      rootStyle.setProperty('--app-height', Math.round(height) + 'px');
      rootStyle.setProperty('--app-top', Math.round(top) + 'px');
    });
  }

  function bindViewport() {
    syncViewport();
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', syncViewport);
      window.visualViewport.addEventListener('scroll', syncViewport);
    }
    window.addEventListener('resize', syncViewport);
    // Страница сама по себе не прокручивается: если браузер сдвинул её к полю ввода, возвращаем.
    window.addEventListener('scroll', () => {
      if (window.scrollY !== 0 && !(window.visualViewport && window.visualViewport.offsetTop > 0)) window.scrollTo(0, 0);
      syncViewport();
    }, { passive: true });
    dom.input.addEventListener('focus', () => setTimeout(syncViewport, 350));
    dom.input.addEventListener('blur', () => {
      syncViewport();
      setTimeout(syncViewport, 350);
    });
    if (IN_TELEGRAM) tg.onEvent('viewportChanged', syncViewport);
  }

  function start() {
    state.theme = store(THEME_KEY) || 'auto';
    applyTheme();
    bindViewport();
    bindEvents();

    if (IN_TELEGRAM) {
      tg.ready();
      tg.expand();
      if (supports('7.7')) tg.disableVerticalSwipes(); // прокрутка ленты не должна сворачивать приложение
      // Кнопка «Назад» Telegram: из инструкций — к настройкам, из настроек — закрыть.
      if (supports('6.1')) tg.BackButton.onClick(() => (state.sheetPage === 'instructions' ? leaveInstructionsPage() : closeSheet()));
      tg.onEvent('themeChanged', () => applyTheme({ x: window.innerWidth / 2, y: 0 }));
    } else {
      const scheme = window.matchMedia('(prefers-color-scheme: light)');
      if (scheme.addEventListener) scheme.addEventListener('change', () => applyTheme({ x: window.innerWidth / 2, y: 0 }));
    }

    if (!DEMO && !IN_TELEGRAM) {
      showGate(
        'Открой меня в Telegram',
        'Это приложение работает внутри Telegram: зайди в чат с ботом и нажми кнопку меню рядом с полем ввода.'
      );
      return;
    }
    boot();
  }

  start();
})();
