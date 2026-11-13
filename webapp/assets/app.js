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

  function applyTheme() {
    const forced = query.get('theme');
    let scheme;
    if (forced === 'light' || forced === 'dark') scheme = forced;
    else if (IN_TELEGRAM) scheme = tg.colorScheme;
    else scheme = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';

    const root = document.documentElement;
    root.dataset.theme = scheme === 'light' ? 'light' : 'dark';
    const bg = getComputedStyle(root).getPropertyValue('--bg').trim();
    try {
      if (supports('6.9')) tg.setHeaderColor(bg);
      if (supports('6.1')) tg.setBackgroundColor(bg);
      if (supports('7.10')) tg.setBottomBarColor(bg);
    } catch (error) { /* старый клиент Telegram */ }
  }

  /* ---------- Сервер ---------- */

  class ApiError extends Error {
    constructor(code, message, status) {
      super(message);
      this.code = code;
      this.status = status || 0;
    }
  }

  async function request(path, body) {
    let response;
    try {
      response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: Object.assign(
          { Authorization: 'tma ' + tg.initData },
          body === undefined ? {} : { 'Content-Type': 'application/json' }
        ),
        body: body === undefined ? undefined : JSON.stringify(body),
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
    clear: () => request('api/clear', {}),
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
        };
      },
      async chat(text, model) {
        await sleep(1100 + Math.random() * 700);
        if (/ошибк/i.test(text)) {
          throw new ApiError('ai_unavailable', 'ИИ-сервис сейчас не отвечает. Отправь сообщение ещё раз чуть позже.', 503);
        }
        const content = replies[turn % replies.length];
        turn += 1;
        used += 1;
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

    const root = el('article', 'msg msg--' + message.role + (fresh ? ' msg--new' : ''));
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
    } else {
      bubble.textContent = message.content;
    }

    root.append(bubble, meta);
    dom.messages.append(root);
    return { root, bubble };
  }

  /* Ответ появляется постепенно, как будто Эля печатает: пузырь плавно растёт,
     в конце текста мерцает курсор, лента сама следует за ответом. */
  function reveal(root, bubble, text, onDone) {
    const full = () => window.ElyaMarkdown.render(text);
    if (reducedMotion || text.length < 30) {
      bubble.replaceChildren(full());
      onDone();
      return null;
    }

    const step = Math.max(3, Math.ceil(text.length / 90));
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

    function finish() {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      cancelAnimationFrame(frame);
      render(text);
      const settle = () => {
        root.classList.remove('is-streaming');
        if (state.stick && !userIsScrolling()) scrollToBottom(false);
      };
      if (grow) grow.onfinish = settle;
      else settle();
      onDone();
    }

    function tick() {
      shown = Math.min(text.length, shown + step);
      // Не обрываем слово посередине.
      for (let extra = 0; extra < 24 && shown < text.length && !/\s/.test(text[shown]); extra += 1) shown += 1;
      if (shown >= text.length) {
        finish();
        return;
      }
      render(text.slice(0, shown));
      timer = setTimeout(tick, 28);
    }

    tick();
    frame = requestAnimationFrame(pin);
    return finish;
  }

  function addAssistantReply(reply) {
    const item = addMessage(reply, { fresh: true, reveal: true });
    state.finishReveal = reveal(item.root, item.bubble, reply.content, () => {
      state.finishReveal = null;
      if (reply.truncated) addContinueButton();
    });
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

  function updateComposer() {
    const length = dom.input.value.length;
    dom.send.disabled = state.busy || !dom.input.value.trim();
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

  async function send(rawText, failedItem) {
    const text = String(rawText).trim();
    if (!text || state.busy) return;

    if (state.finishReveal) state.finishReveal();
    clearNotice();
    setBusy(true);
    haptic('impact', 'light');

    let item = failedItem;
    if (item) {
      item.root.classList.remove('msg--failed');
    } else {
      item = addMessage({ role: 'user', content: text, ts: nowTs() }, { fresh: true });
    }
    updateWelcome();
    const typing = showTyping();
    scrollToBottom(true);

    try {
      const data = await backend.chat(text, state.model);
      typing.dismiss();
      addAssistantReply(data.reply);
      haptic('impact', 'soft');
      if (data.usage) state.usage = data.usage;
      state.signature = '';
      state.syncedAt = Date.now();
      setBusy(false);
    } catch (error) {
      typing.dismiss();
      setBusy(false);
      haptic('notify', 'error');
      if (error.status === 401 || error.status === 403) {
        showLoadError(error, boot);
        return;
      }
      if (error.code === 'network') setStatus('offline');
      item.root.classList.add('msg--failed');
      showNotice(error.message, () => send(text, item));
      scrollToBottom(true);
    }
  }

  function submit() {
    const text = dom.input.value;
    if (!text.trim() || state.busy) return;
    dom.input.value = '';
    autosize();
    updateComposer();
    send(text);
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

  function openSheet() {
    renderModels();
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
  }

  function closeSheet() {
    if (dom.sheet.hidden || state.sheetClosing) return;
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
      dom.sheet.classList.remove('is-opening', 'is-dragging');
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
    dom.app.hidden = false;
    if (!reducedMotion) {
      dom.app.classList.add('is-entering');
      setTimeout(() => dom.app.classList.remove('is-entering'), 1100);
    }
    setStatus('online');
    renderHistory(data.history || []);
    updateComposer();
    hideGate();
    if (finePointer) dom.input.focus();
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

    dom.suggestions.addEventListener('click', (event) => {
      const card = event.target.closest('.suggestion');
      if (card) send(card.dataset.prompt);
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

    dom.menuBtn.addEventListener('click', openSheet);
    dom.sheetClose.addEventListener('click', closeSheet);
    dom.backdrop.addEventListener('click', closeSheet);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeSheet();
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

  function start() {
    applyTheme();
    bindEvents();

    if (IN_TELEGRAM) {
      tg.ready();
      tg.expand();
      if (supports('7.7')) tg.disableVerticalSwipes(); // прокрутка ленты не должна сворачивать приложение
      if (supports('6.1')) tg.BackButton.onClick(closeSheet);
      tg.onEvent('themeChanged', applyTheme);
    } else {
      const scheme = window.matchMedia('(prefers-color-scheme: light)');
      if (scheme.addEventListener) scheme.addEventListener('change', applyTheme);
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
