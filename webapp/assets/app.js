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
    setTimeout(() => { dom.gate.hidden = true; }, reducedMotion ? 0 : 300);
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
    dom.scrollDown.hidden = distance < 320;
  }

  /* ---------- Сообщения ---------- */

  function modelTitle(model) {
    return String(model).split('/').pop().replace(/:free$/, '');
  }

  function setStatus(kind) {
    const labels = { online: 'на связи', typing: 'печатает…', offline: 'нет связи' };
    dom.status.dataset.state = kind;
    dom.statusLabel.textContent = labels[kind];
    dom.avatar.classList.toggle('orb--thinking', kind === 'typing');
  }

  function updateWelcome() {
    const empty = dom.messages.childElementCount === 0;
    dom.welcome.hidden = !empty;
    if (empty) dom.welcomeTitle.textContent = greeting();
  }

  function addDay(ts) {
    const date = new Date(ts * 1000);
    const key = dayKey(date);
    if (key === state.lastDay) return;
    state.lastDay = key;
    dom.messages.append(el('div', 'day', dayLabel(date)));
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
    addDay(message.ts);

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

  /* Ответ появляется постепенно, как будто Эля печатает. */
  function reveal(bubble, text, onDone) {
    const full = () => window.ElyaMarkdown.render(text);
    if (reducedMotion || text.length < 30) {
      bubble.replaceChildren(full());
      onDone();
      return null;
    }

    const step = Math.max(3, Math.ceil(text.length / 80));
    let shown = 0;
    let timer = null;
    let finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      const stick = state.stick;
      bubble.replaceChildren(full());
      if (stick) scrollToBottom(false);
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
      const stick = state.stick;
      bubble.replaceChildren(window.ElyaMarkdown.render(text.slice(0, shown)));
      if (stick) scrollToBottom(false);
      timer = setTimeout(tick, 26);
    }

    tick();
    return finish;
  }

  function addAssistantReply(reply) {
    const item = addMessage(reply, { fresh: true, reveal: true });
    state.finishReveal = reveal(item.bubble, reply.content, () => {
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

  function showTyping() {
    const root = el('article', 'msg msg--assistant msg--new typing');
    const bubble = el('div', 'bubble');
    bubble.append(el('span'), el('span'), el('span'));
    bubble.setAttribute('aria-label', state.assistantName + ' печатает');
    root.append(bubble);
    dom.messages.append(root);
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
    state.busy = busy;
    dom.send.classList.toggle('is-busy', busy);
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
      typing.remove();
      addAssistantReply(data.reply);
      if (data.usage) state.usage = data.usage;
      state.signature = '';
      state.syncedAt = Date.now();
      setBusy(false);
    } catch (error) {
      typing.remove();
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
    const saved = Array.from(button.childNodes);
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
    dom.toast.hidden = true;
    dom.toast.textContent = text;
    void dom.toast.offsetWidth; // перезапуск анимации
    dom.toast.hidden = false;
    toastTimer = setTimeout(() => { dom.toast.hidden = true; }, 2400);
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
    dom.usageValue.textContent = String(used);
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
    dom.sheet.classList.remove('is-closing');
    dom.backdrop.classList.remove('is-closing');
    dom.backdrop.hidden = false;
    dom.sheet.hidden = false;
    dom.input.blur();
    // Полоска заполняется уже на глазах.
    dom.usageFill.style.width = '0';
    requestAnimationFrame(renderUsage);
    if (supports('6.1')) tg.BackButton.show();
    haptic('impact', 'light');
  }

  function closeSheet() {
    if (dom.sheet.hidden || dom.sheet.classList.contains('is-closing')) return;
    dom.sheet.classList.add('is-closing');
    dom.backdrop.classList.add('is-closing');
    if (supports('6.1')) tg.BackButton.hide();
    setTimeout(() => {
      dom.sheet.hidden = true;
      dom.backdrop.hidden = true;
    }, reducedMotion ? 0 : 200);
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
    renderHistory([]);
    closeSheet();
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
