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
    searchToggle: $('search-toggle'),
    fileInput: $('file-input'),
    attachment: $('attachment'),
    attachmentImg: $('attachment-img'),
    attachmentRemove: $('attachment-remove'),
    attachmentFile: $('attachment-file'),
    attachmentIcon: $('attachment-icon'),
    attachmentName: $('attachment-name'),
    attachmentSize: $('attachment-size'),
    docsBar: $('docs-bar'),
    githubBlock: $('github-block'),
    githubStatus: $('github-status'),
    githubConnect: $('github-connect'),
    githubToken: $('github-token'),
    githubConnectBtn: $('github-connect-btn'),
    githubRepo: $('github-repo'),
    githubRepos: $('github-repos'),
    githubNewName: $('github-new-name'),
    githubCreate: $('github-create'),
    githubDisconnect: $('github-disconnect'),
    docsList: $('docs-list'),
    videoBar: $('video-bar'),
    videoBarText: $('video-bar-text'),
    videoDone: $('video-done'),
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
    codeBlock: $('code-block'),
    codeToggle: $('code-toggle'),
    codeSub: $('code-sub'),
    instructionsBlock: $('instructions-block'),
    openInstructions: $('open-instructions'),
    instructionsSummary: $('instructions-summary'),
    instructionsPage: $('instructions-page'),
    aboutBtn: $('about-btn'),
    aboutPage: $('about-page'),
    aboutLines: $('about-lines'),
    aboutLinesLabel: $('about-lines-label'),
    aboutAuthor: $('about-author'),
    instructionsToggle: $('instructions-toggle'),
    instructionsStatus: $('instructions-status'),
    instrAbout: $('instr-about'),
    instrStyle: $('instr-style'),
    instrAboutCount: $('instr-about-count'),
    instrStyleCount: $('instr-style-count'),
    instrSave: $('instr-save'),
    remindersBlock: $('reminders-block'),
    memoryBlock: $('memory-block'),
    drawerBtn: $('drawer-btn'),
    drawer: $('drawer'),
    drawerBackdrop: $('drawer-backdrop'),
    drawerClose: $('drawer-close'),
    drawerTitle: $('drawer-title'),
    drawerScroll: $('drawer-scroll'),
    drawerDraw: $('drawer-draw'),
    drawerVideo: $('drawer-video'),
    drawerVideoMeta: $('drawer-video-meta'),
    drawerCode: $('drawer-code'),
    drawerCodeMeta: $('drawer-code-meta'),
    drawerReminders: $('drawer-reminders'),
    drawerMemory: $('drawer-memory'),
    drawerInstructions: $('drawer-instructions'),
    drawerInstructionsMeta: $('drawer-instructions-meta'),
    drawerEmpty: $('drawer-empty'),
    projectChips: $('project-chips'),
    projectSettings: $('project-settings'),
    chatList: $('chat-list'),
    chatsLabel: $('chats-label'),
    newChatBtn: $('new-chat-btn'),
    topbarProject: $('topbar-project'),
    projectPage: $('project-page'),
    projectEmojis: $('project-emojis'),
    projectName: $('project-name'),
    projectInstructions: $('project-instructions'),
    projectInstructionsCount: $('project-instructions-count'),
    projectCode: $('project-code'),
    projectCodeBlock: $('project-code-block'),
    projectFilesBlock: $('project-files-block'),
    projectFiles: $('project-files'),
    projectFilesEmpty: $('project-files-empty'),
    projectFileAdd: $('project-file-add'),
    projectFileInput: $('project-file-input'),
    projectDeleteBlock: $('project-delete-block'),
    projectDelete: $('project-delete'),
    projectDeleteConfirm: $('project-delete-confirm'),
    projectDeleteCancel: $('project-delete-cancel'),
    projectDeleteYes: $('project-delete-yes'),
    projectSave: $('project-save'),
    drawerNew: $('drawer-new'),
    drawerMe: $('drawer-me'),
    drawerInitials: $('drawer-initials'),
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
    search: false, // на сервере подключён поиск в интернете
    searchNext: false, // кнопка 🌐: следующее сообщение — с поиском
    attachment: null,
    documents: null, // документы разговора; null — сервер их не читает
    // Монтаж видео: available — есть ffmpeg на сервере; session — идёт монтаж (общий с чатом бота).
    video: { available: false, uploadLimitMb: 100, session: null, working: false },
    recording: null,
    transcribing: false,
    reminders: { timezone: null, items: [], max: 50, max_text: 300 },
    mode: null, // 'chat' | 'code'; null — сервер не знает о режимах
    instructions: null,
    instructionsEdited: false, // пользователь правил поля и ещё не сохранил
    sheetPage: 'main',
    clearArmed: null,
    images: { enabled: false },
    drawerAnim: null,
    drawerClosing: false,
    preview: null, // открытый предпросмотр HTML
    github: null, // состояние GitHub; null — выключен на сервере
    githubRepos: null,
    chat: null, // открытый чат
    chats: [],
    projects: [],
    chatFilter: 'all', // какие чаты показывать в шторке: 'all' или id проекта
    chatMenu: null, // у какого чата открыты действия
    chatRename: null,
    chatDeleteAsk: null,
    projectEdit: null, // проект на странице настройки ({ id: null } — новый)
    drawerDragged: false,
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

  let chromeTimer = null;

  /* Цвета шапки и низа Telegram. delay — когда их сменить: их нельзя анимировать,
     поэтому при плавной смене темы они меняются на середине перехода, а не рывком в начале. */
  function paintChrome(delay) {
    clearTimeout(chromeTimer);
    const run = () => {
      const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
      try {
        if (supports('6.9')) tg.setHeaderColor(bg);
        if (supports('6.1')) tg.setBackgroundColor(bg);
        if (supports('7.10')) tg.setBottomBarColor(bg);
      } catch (error) { /* старый клиент Telegram */ }
    };
    if (delay) chromeTimer = setTimeout(run, delay);
    else run();
  }

  function paintTheme(scheme, chromeDelay) {
    document.documentElement.dataset.theme = scheme;
    paintChrome(chromeDelay);
  }

  const THEME_FADE_MS = 520;
  let themeFadeTimer = null;
  let themeTransition = null;

  /* origin — признак смены по действию пользователя: тогда тема перетекает плавно;
     без него меняется сразу. */
  function applyTheme(origin) {
    const root = document.documentElement;
    const scheme = resolveScheme();
    if (root.dataset.themeReady && root.dataset.theme === scheme) return;
    root.dataset.themeReady = '1';

    if (!origin || reducedMotion) {
      paintTheme(scheme);
      return;
    }

    // Быстрое повторное переключение: прошлый переход сразу завершаем.
    if (themeTransition) {
      try { themeTransition.skipTransition(); } catch (error) { /* уже закончился */ }
    }

    if (typeof document.startViewTransition === 'function') {
      // Старый и новый кадры плавно растворяются друг в друге. Анимируется только
      // прозрачность снимков — это делает видеокарта, без перерисовки страницы.
      // Собственные CSS-переходы цветов на время смены выключены, иначе внутри
      // нового кадра они догоняли бы тему ещё раз и давали рывок.
      root.classList.add('theme-switching');
      const transition = document.startViewTransition(() => paintTheme(scheme, THEME_FADE_MS / 2));
      themeTransition = transition;
      transition.finished.finally(() => {
        if (themeTransition === transition) themeTransition = null;
        root.classList.remove('theme-switching');
      });
      return;
    }

    // Без View Transitions цвета просто плавно перетекают.
    root.classList.add('theme-fade');
    paintTheme(scheme, THEME_FADE_MS / 2);
    clearTimeout(themeFadeTimer);
    themeFadeTimer = setTimeout(() => root.classList.remove('theme-fade'), THEME_FADE_MS + 80);
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

  function chatBody(text, model, search, documents) {
    const body = { text };
    if (model !== AUTO) body.model = model;
    if (search) body.search = true;
    if (documents && documents.length) body.documents = documents;
    return body;
  }

  const server = {
    state: () => request('api/state'),
    chat: (text, model, search) => request('api/chat', chatBody(text, model, search)),
    /* Ответ по мере генерации (NDJSON). onDelta получает новые куски текста;
       extra.search — искать в интернете, extra.onSearch(query) — Эля начала искать.
       Возвращает то же, что chat(). Без поддержки потоков — обычный chat(). */
    async chatStream(text, model, onDelta, extra) {
      const search = Boolean(extra && extra.search);
      if (!window.ReadableStream || !window.TextDecoder) return server.chat(text, model, search);
      let response;
      try {
        response = await fetch('api/chat/stream', {
          method: 'POST',
          headers: { Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json' },
          body: JSON.stringify(chatBody(text, model, search, extra && extra.documents)),
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
        else if (event.type === 'status') {
          if (extra && extra.onStatus) extra.onStatus(String(event.text || ''));
        } else if (event.type === 'search') {
          if (extra && extra.onSearch) extra.onSearch(String(event.query || ''));
        } else if (event.type === 'run') {
          if (extra && extra.onRun) extra.onRun(Number(event.number) || 1);
        } else if (event.type === 'done') result = event;
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
    /* projectId: undefined — в том же проекте, null — без проекта, число — в проекте. */
    newChat: (projectId) => request('api/chat/new', projectId === undefined ? {} : { project_id: projectId }),
    chatOpen: (id) => request('api/chat/open', { id }),
    chatRename: (id, title) => request('api/chat/rename', { id, title }),
    chatDelete: (id) => request('api/chat/delete', { id }),
    projectSave: (data) => request('api/projects/save', data),
    projectDelete: (id) => request('api/projects/delete', { id }),
    projectFiles: (id) => request('api/projects/files', { id }),
    projectFileUpload: (id, file) => uploadFile('api/document', file, () => {}, { project_id: String(id) }),
    savePreferences: (patch) => request('api/preferences', patch),
    saveInstructions: (patch) => request('api/instructions', patch),
    setMode: (mode) => request('api/mode', { mode }),
    memorySet: (enabled) => request('api/memory', { enabled }),
    memoryAdd: (text) => request('api/memory/add', { text }),
    memoryDelete: (id) => request('api/memory/delete', { id }),
    memoryClear: () => request('api/memory/delete', { all: true }),
    setTimezone: (tz) => request('api/timezone', { tz }),
    voice: (blob, type) => request('api/voice', blob, type),
    documentUpload: (file, onProgress) => uploadFile('api/document', file, onProgress),
    documentsDelete: (id) => request('api/documents/delete', id === 'all' ? { all: true } : { id }),
    photo: (text, blob, model) => {
      const form = new FormData();
      if (text) form.append('text', text);
      if (model && model !== AUTO) form.append('model', model);
      form.append('image', blob, 'photo.jpg');
      return request('api/photo', form);
    },
    reminderAdd: (text, when, repeat) => request('api/reminders/add', { text, when, repeat }),
    reminderDelete: (id) => request('api/reminders/delete', { id }),
    videoStart: () => request('api/video/start', {}),
    videoUpload: (file, onProgress) => uploadFile('api/video/upload', file, onProgress),
    videoEdit: (text, onProgress) => streamEvents('api/video/edit', { text }, onProgress),
    videoAction(action, value, onProgress) {
      if (action === 'done') return request('api/video/action', { action });
      return streamEvents('api/video/action', { action, value }, onProgress);
    },
    async videoResult(name) {
      let response;
      try {
        response = await fetch('api/video/result/' + encodeURIComponent(name), {
          headers: { Authorization: 'tma ' + tg.initData },
        });
      } catch (error) {
        throw new ApiError('network', 'Нет связи с сервером. Видео есть в чате с ботом.');
      }
      if (!response.ok) throw new ApiError('http_' + response.status, 'Этого варианта уже нет — он в чате с ботом.', response.status);
      return response.blob();
    },
    drawAgain: (id) => request('api/draw/again', { id }),
    drawSelect: (id) => request('api/draw/select', { id }),
    fileInfo: (token) => request('api/files/info', { token }),
    github: (body) => request('api/github', body),
  };

  /* Ошибка из ответа сервера (JSON с полем error) или по статусу. */
  async function responseError(response) {
    let data = null;
    try {
      data = await response.json();
    } catch (error) { /* не JSON */ }
    const info = (data && data.error) || {};
    return new ApiError(info.code || 'http_' + response.status,
      info.message || 'Сервер ответил ошибкой. Попробуй ещё раз чуть позже.', response.status);
  }

  /* Долгая операция с ходом работы построчно (NDJSON): progress … ping … done.
     onProgress получает текст этапа; возвращается событие done. */
  async function streamEvents(path, body, onProgress) {
    let response;
    try {
      response = await fetch(path, {
        method: 'POST',
        headers: { Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (error) {
      throw new ApiError('network', 'Нет связи с сервером. Проверь интернет и попробуй ещё раз.');
    }
    if (!response.ok || !response.body || !window.TextDecoder) throw await responseError(response);
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
      if (event.type === 'progress' && event.text) onProgress(event.text);
      else if (event.type === 'done') result = event;
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
      throw new ApiError('network', 'Связь прервалась. Монтаж продолжится — готовое видео придёт в чат с ботом.');
    }
    if (!result) throw new ApiError('network', 'Связь прервалась. Монтаж продолжится — готовое видео придёт в чат с ботом.');
    return result;
  }

  /* Загрузка файла с процентами (у fetch их нет). */
  function uploadFile(path, file, onProgress, fields) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      Object.entries(fields || {}).forEach(([key, value]) => form.append(key, value));
      form.append('file', file, file.name || 'video.mp4');
      xhr.open('POST', path);
      xhr.setRequestHeader('Authorization', 'tma ' + tg.initData);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(Math.min(99, Math.round((event.loaded / event.total) * 100)));
      };
      xhr.onerror = () => reject(new ApiError('network', 'Не получилось загрузить файл: нет связи. Попробуй ещё раз.'));
      xhr.onload = () => {
        let data = null;
        try {
          data = JSON.parse(xhr.responseText);
        } catch (error) { /* не JSON */ }
        if (xhr.status >= 200 && xhr.status < 300 && data) {
          resolve(data);
          return;
        }
        const info = (data && data.error) || {};
        reject(new ApiError(info.code || 'http_' + xhr.status,
          info.message || 'Сервер не принял файл. Попробуй ещё раз.', xhr.status));
      };
      xhr.send(form);
    });
  }

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
    let drawingId = 0;
    let docs = [];
    let docId = 0;
    let github = { connected: false, login: '', repo: '', branch: '', from_env: false };
    // Чаты и проекты в демо: всё в памяти страницы.
    let projects = [
      { id: 1, name: 'Учёба', emoji: '📚', instructions: 'Объясняй понятно и по шагам, с примерами.', mode: '' },
      { id: 2, name: 'Код', emoji: '💻', instructions: '', mode: 'code' },
      { id: 3, name: 'Личное', emoji: '🌿', instructions: '', mode: '' },
    ];
    let projectId = 3;
    const projectFiles = {};
    let chatId = 1;
    let chats = [{ id: 1, project_id: null, title: history.length ? 'Привет! Что ты умеешь?' : '', updated: nowTs(), history }];
    let activeChat = chats[0];
    const effectiveMode = () => {
      const project = projects.find((p) => p.id === activeChat.project_id);
      return project && project.mode === 'code' ? 'code' : mode;
    };
    const chatView = (chat) => ({ id: chat.id, project_id: chat.project_id, title: chat.title, updated: chat.updated });
    function chatsView(withHistory) {
      activeChat.history = history;
      const list = chats
        .filter((chat) => chat === activeChat || chat.history.length)
        .sort((a, b) => b.updated - a.updated)
        .map(chatView);
      const view = {
        chat: chatView(activeChat), chats: list, projects: projects.map((p) => Object.assign({}, p)),
        documents: docs.slice(), mode: effectiveMode(),
        limits: { max_text_length: effectiveMode() === 'code' ? 16000 : 4000 },
      };
      if (withHistory) view.history = history.slice();
      return view;
    }
    function touchChat(text) {
      activeChat.updated = nowTs();
      if (!activeChat.title && text) activeChat.title = text.split('\n')[0].slice(0, 50);
    }
    function openDemoChat(chat) {
      activeChat.history = history;
      activeChat = chat;
      history = chat.history;
      docs = chat.docs || (chat.docs = []);
    }

    /* В демо «рисунок» — случайный градиент с кругами: так видно, как выглядит ответ с картинкой. */
    function demoDrawing(request, id) {
      const hue = Math.floor(Math.random() * 360);
      const circles = Array.from({ length: 6 }, (_, i) => {
        const x = 80 + Math.random() * 360;
        const y = 80 + Math.random() * 360;
        const r = 30 + Math.random() * 90;
        return '<circle cx="' + x.toFixed(0) + '" cy="' + y.toFixed(0) + '" r="' + r.toFixed(0) +
          '" fill="hsl(' + ((hue + i * 40) % 360) + ' 80% 70% / 0.55)"/>';
      }).join('');
      const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">' +
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(' + hue +
        ' 70% 45%)"/><stop offset="1" stop-color="hsl(' + ((hue + 90) % 360) + ' 70% 25%)"/></linearGradient></defs>' +
        '<rect width="512" height="512" fill="url(#g)"/>' + circles + '</svg>';
      const content = '🎨 Нарисовала картинку: демо-рисунок\n\n_Промпт: в демо-режиме я рисую узоры, а в Telegram — настоящую картинку по описанию_';
      history.push({ role: 'user', content: request, ts: nowTs() }, { role: 'assistant', content, ts: nowTs() });
      return {
        reply: {
          role: 'assistant', content, ts: nowTs(), provider: 'images', model: 'demo', truncated: false,
          image: 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg), drawing_id: id, sent_to_chat: false,
        },
        usage: { used, limit: 300 },
      };
    }
    let preferences = { style: 'friendly', verbosity: 'normal' };
    let instructions = { about: '', style: '', enabled: true, max_length: 1500 };
    let mode = 'chat';
    let memory = { enabled: false, facts: [], max_facts: 50, max_fact_length: 200 };
    let factId = 0;
    let reminders = { timezone: null, items: [], max: 50, max_text: 300 };
    let reminderId = 0;
    const remindersView = () => JSON.parse(JSON.stringify(reminders));
    const memoryView = () => JSON.parse(JSON.stringify(memory));
    // Монтаж в демо: настоящего ffmpeg нет, видео не загружается, обложка рисуется на холсте.
    let video = null;
    const videoView = () => {
      if (!video) return null;
      const actions = [
        { action: 'quick', value: 'pauses', label: video.flags.pauses ? '🗣 Вернуть паузы' : '🤐 Убрать паузы' },
        { action: 'quick', value: 'subs', label: video.flags.subs ? '💬 Без субтитров' : '💬 Субтитры' },
        { action: 'quick', value: 'vertical', label: video.flags.vertical ? '🖥 Исходный формат' : '📱 Вертикально 9:16' },
        { action: 'quick', value: 'circle', label: '⭕ Кружочек' },
      ];
      if (video.history.length > 1) actions.push({ action: 'undo', value: '', label: '↩️ Отменить' });
      return { clips: video.clips.slice(), music: null, has_result: video.rendered, busy: false, actions };
    };
    const demoPanel = () => '📥 **Клипы**\n' + video.clips.map((clip, i) => (i + 1) + ') ' + clip.label).join('\n') +
      '\n\nНажми кнопку или напиши своими словами — например, _«обрежь первые 5 секунд»_ или _«сделай вертикально и добавь субтитры»_.';
    function demoPoster(vertical) {
      const canvas = document.createElement('canvas');
      canvas.width = vertical ? 270 : 480;
      canvas.height = vertical ? 480 : 270;
      const ctx = canvas.getContext('2d');
      const gradient = ctx.createLinearGradient(0, 0, canvas.width, canvas.height);
      gradient.addColorStop(0, '#ff8f70');
      gradient.addColorStop(0.5, '#ff5f9e');
      gradient.addColorStop(1, '#9b6bff');
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = 'rgba(255,255,255,0.92)';
      ctx.font = 'bold 26px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Демо-видео', canvas.width / 2, canvas.height / 2);
      return canvas.toDataURL('image/jpeg', 0.8);
    }
    async function demoRender(lines, onProgress) {
      const steps = ['🤔 Думаю, как смонтировать…', '🎞 Готовлю фрагменты: 1 из 3', '🎞 Готовлю фрагменты: 3 из 3', '🎬 Собираю видео…'];
      for (const step of steps) {
        onProgress(step);
        await sleep(650);
      }
      video.history.push(Object.assign({}, video.flags));
      video.rendered = true;
      const text = '🎬 **Готово** · 0:23 · ' + (video.flags.vertical ? '720×1280' : '1280×720') + '\n\n' + lines.join('\n') +
        '\n\nФайл — в чате с ботом. Что поправить? Напиши, например, «ускорь в 1,5 раза».';
      return {
        outcome: { text, error: false, rendered: true, kind: 'video', duration: 23.4, width: 720, height: 1280,
          result: 'result-demo.mp4', poster: demoPoster(video.flags.vertical) },
        session: videoView(),
      };
    }

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
          limits: { max_text_length: mode === 'code' ? 16000 : 4000 },
          mode,
          preferences: Object.assign({}, preferences),
          instructions: Object.assign({}, instructions),
          memory: memoryView(),
          reminders: remindersView(),
          voice: true,
          vision: true,
          video: { available: true, upload_limit_mb: 100, session: videoView() },
          images: { enabled: true, edit: true, used: 0, limit: 50 },
          documents: docs.slice(),
          github: Object.assign({}, github),
          chat: chatsView().chat,
          chats: chatsView().chats,
          projects: chatsView().projects,
          search: true,
          about: { code_lines: 31836 },
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
      async setMode(next) {
        await sleep(200);
        mode = next;
        const effective = effectiveMode();
        const data = { mode: effective, limits: { max_text_length: effective === 'code' ? 16000 : 4000 } };
        if (effective !== next) data.locked = true;
        return data;
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
        if (/убер|удали|замени|перекрас|добавь|сделай|измени/i.test(text || '')) {
          drawingId += 1;
          return demoDrawing('📷 Фото: ' + text, drawingId);
        }
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
      async videoStart() {
        await sleep(250);
        if (video) return { message: demoPanel(), session: videoView() };
        video = { clips: [], flags: { pauses: false, subs: false, vertical: false }, history: [], rendered: false };
        return {
          message: '🎬 **Монтаж видео**\n\nПрикрепи видео скрепкой 📎 и напиши, что сделать:\n- _обрежь первые 5 секунд и ускорь в 1,5 раза_\n- _убери паузы и добавь субтитры_\n- _сделай вертикально для Reels и подпиши «Лето 2026»_',
          session: videoView(),
        };
      },
      async videoUpload(file, onProgress) {
        for (let pct = 12; pct < 100; pct += 29) {
          onProgress(pct);
          await sleep(220);
        }
        if (!video) video = { clips: [], flags: { pauses: false, subs: false, vertical: false }, history: [], rendered: false };
        video.clips.push({ duration: 14.2, label: '0:14, вертикальное' });
        return { message: demoPanel(), session: videoView() };
      },
      async videoEdit(text, onProgress) {
        used += 1;
        const lines = [];
        if (/ускор/i.test(text)) lines.push('✂️ Фрагмент: целиком ×1.5');
        if (/обреж/i.test(text)) lines.push('✂️ Фрагмент: 0:05–0:14');
        if (/вертик|рилс|reels/i.test(text)) {
          video.flags.vertical = true;
          lines.push('📐 Формат вертикальный 9:16, размытый фон');
        }
        if (/субтитр/i.test(text)) {
          video.flags.subs = true;
          lines.push('💬 Субтитры из речи');
        }
        if (!lines.length) lines.push('🎨 Фильтр: сочные цвета');
        return demoRender(lines, onProgress);
      },
      async videoAction(action, value, onProgress) {
        if (action === 'done') {
          await sleep(200);
          video = null;
          return { message: '✅ Монтаж закончен, файлы удалила. Захочешь ещё — просто пришли видео 🎬', session: null };
        }
        if (action === 'undo') {
          video.flags = video.history.length > 1 ? video.history[video.history.length - 2] : { pauses: false, subs: false, vertical: false };
          video.history.pop();
          return demoRender(['↩️ Вернула прошлый вариант'], onProgress);
        }
        const names = { pauses: '🤐 Паузы вырезаны (−0:04)', subs: '💬 Субтитры из речи', vertical: '📐 Формат вертикальный 9:16, размытый фон', circle: '⭕ Кружок' };
        if (value in video.flags) video.flags[value] = !video.flags[value];
        return demoRender([names[value] || '✨ Готово'], onProgress);
      },
      async videoResult() {
        await sleep(200);
        throw new ApiError('demo', 'В демо-режиме видео не настоящее — в Telegram здесь включится плеер.');
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
      async github(body) {
        await sleep(400);
        if (body.action === 'connect') github = { connected: true, login: 'demo-user', repo: '', branch: '', from_env: false };
        else if (body.action === 'disconnect') github = { connected: false, login: '', repo: '', branch: '', from_env: false };
        else if (body.action === 'repos') return { repos: ['demo-user/my-telegram-bot', 'demo-user/notes'], github };
        else if (body.action === 'use') github = Object.assign({}, github, { repo: body.repo, branch: 'main' });
        else if (body.action === 'create') github = Object.assign({}, github, { repo: 'demo-user/' + body.name, branch: 'main' });
        else if (body.action === 'push') {
          return { github, pushed: { repo: github.repo || 'demo-user/my-telegram-bot', files: 3, commit_url: 'https://github.com', tree_url: 'https://github.com' } };
        }
        return { github: Object.assign({}, github) };
      },
      async drawSelect(id) {
        await sleep(100);
        return { selected: id };
      },
      async drawAgain(id) {
        await sleep(1500);
        return demoDrawing('🔄 Ещё вариант', id);
      },
      async documentUpload(file) {
        await sleep(900);
        docId += 1;
        const document = { id: docId, name: file.name, kind: 'pdf', info: 'PDF, 12 стр.' };
        docs.push(document);
        activeChat.docs = docs;
        return { document, truncated: false, documents: docs.slice() };
      },
      async documentsDelete(id) {
        await sleep(200);
        docs = id === 'all' ? [] : docs.filter((doc) => doc.id !== id);
        activeChat.docs = docs;
        Object.keys(projectFiles).forEach((key) => {
          projectFiles[key] = projectFiles[key].filter((doc) => doc.id !== id);
        });
        return { documents: docs.slice() };
      },
      async chatStream(text, model, onDelta, extra) {
        touchChat(text || ((extra && extra.documents && extra.documents.length) ? '📄 Документ' : ''));
        if (extra && extra.documents && extra.documents.length) {
          await sleep(1200);
          const name = (docs.find((doc) => doc.id === extra.documents[0]) || { name: 'документ' }).name;
          const content = (text ? 'Нашла в «' + name + '» (стр. 4): ' : 'Это демо-режим: файл «' + name + '» я по-настоящему не читаю. ') +
            'В Telegram я ищу ответ в тексте документа и ставлю ссылки на страницы — вот так: (стр. 4), (стр. 7).\n\nМожно спросить «что на стр. 5?» или прислать второй файл и написать «сравни их».';
          history.push({ role: 'user', content: '📄 «' + name + '»' + (text ? '\n' + text : ''), ts: nowTs() }, { role: 'assistant', content, ts: nowTs() });
          used += 1;
          for (const piece of content.split(/(\s+)/)) {
            onDelta(piece);
            await sleep(15);
          }
          return { reply: { role: 'assistant', content, ts: nowTs(), provider: 'demo', model: 'gemini-flash-latest', truncated: false }, usage: { used, limit: 300 } };
        }
        if (/график|посчитай|запусти/i.test(text)) return this.demoRun(text, onDelta, extra);
        const searching = Boolean(extra && extra.search) || /гугл|в интернете|источник/i.test(text);
        if (searching && extra && extra.onSearch) {
          await sleep(600);
          extra.onSearch(text.replace(/^(?:по|за)гугли[,:]?\s*|^найди в интернете[,:]?\s*/i, '').slice(0, 80));
          await sleep(1400);
        }
        const data = await this.chat(text, model, searching);
        if (data.reply.provider === 'reminders' || data.reply.image) return data;
        if (/бот/i.test(text) && github.repo) {
          data.reply.push = { id: 1, files: 3, repo: github.repo };
        }
        if (/реферат|доклад|в ворд/i.test(text)) {
          data.reply.files = [{ token: 'demo', name: 'Реферат.docx', kind: 'docx', size: 38400, preview: false }];
        } else if (/сайт|страниц|лендинг/i.test(text)) {
          data.reply.files = [
            { token: 'demo', name: 'sayt-vizitka.zip', kind: 'zip', size: 5200, preview: false },
            { token: 'demo', name: 'index.html', kind: 'html', size: 4100, preview: true },
          ];
        }
        const words = data.reply.content.split(/(\s+)/);
        for (let i = 0; i < words.length; i += 4) {
          onDelta(words.slice(i, i + 4).join(''));
          await sleep(60);
        }
        return data;
      },
      /* Демо запуска кода: блок кода, «Запускаю код…», вывод и график. */
      async demoRun(text, onDelta, extra) {
        await sleep(900);
        const before = 'Построю график и проверю расчёт:\n\n```python\nimport numpy as np\nimport matplotlib.pyplot as plt\n\n' +
          'x = np.linspace(0, 2 * np.pi, 100)\nplt.plot(x, np.sin(x))\nplt.title("Синус")\nprint(round(np.sin(np.pi / 2), 3))\n```';
        const result = '\n\n▶️ **Результат:**\n```text\n1.0\n```\n📈 График `figure_1.png` — ниже.\n\n';
        const after = 'Код отработал без ошибок: sin(π/2) = 1, а график — ниже. В демо-режиме это заготовка, ' +
          'а в Telegram я запускаю код по-настоящему, в песочнице без интернета.';
        const words = before.split(/(\s+)/);
        for (let i = 0; i < words.length; i += 3) {
          onDelta(words.slice(i, i + 3).join(''));
          await sleep(40);
        }
        if (extra && extra.onRun) extra.onRun(1);
        await sleep(1200);
        onDelta(result);
        for (const piece of after.split(/(\s+)/)) {
          onDelta(piece);
          await sleep(25);
        }
        const content = before + result + after;
        history.push({ role: 'user', content: text, ts: nowTs() }, { role: 'assistant', content, ts: nowTs() });
        used += 1;
        return {
          reply: {
            role: 'assistant', content, ts: nowTs(), provider: 'demo', model: 'gemini-flash-latest', truncated: false,
            files: [{ token: 'demo', name: 'figure_1.png', kind: 'image', size: 36000, preview: false }],
          },
          usage: { used, limit: 300 },
        };
      },
      async chat(text, model, searching) {
        await sleep(1100 + Math.random() * 700);
        if (/ошибк/i.test(text)) {
          throw new ApiError('ai_unavailable', 'ИИ-сервис сейчас не отвечает. Отправь сообщение ещё раз чуть позже.', 503);
        }
        if (/нарису|изобрази|картинк/i.test(text)) {
          drawingId += 1;
          return demoDrawing(text, drawingId);
        }
        if (/напомни/i.test(text)) {
          reminderId += 1;
          reminders.items.push({ id: reminderId, text: text.replace(/^.*?напомни\S*\s*/i, '') || 'Напоминание', due: nowTs() + 3600, repeat: 'none' });
          const content = 'Хорошо, напомню через 1 ч: ' + text.replace(/^.*?напомни\S*\s*/i, '') + ' ⏰\nВсе напоминания — в /reminders или в приложении (кнопка 🔔).';
          history.push({ role: 'user', content: text, ts: nowTs() }, { role: 'assistant', content, ts: nowTs() });
          return { reply: { role: 'assistant', content, ts: nowTs(), provider: 'reminders', model: '', truncated: false }, usage: { used, limit: 300 } };
        }
        const content = searching
          ? 'Вот что пишут в интернете [1]. В демо-режиме я не ищу по-настоящему, поэтому ответ и ссылки — заготовка [2].\n\n' +
            '**Источники:**\n1. [Пример результата поиска](https://example.com/) — example.com\n' +
            '2. [Ещё один источник](https://example.org/) — example.org'
          : replies[turn % replies.length];
        if (!searching) turn += 1;
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
        docs = [];
        activeChat.docs = docs;
        activeChat.title = '';
        return Object.assign({ deleted }, chatsView());
      },
      async newChat(project) {
        await sleep(250);
        const target = project === undefined ? activeChat.project_id : project;
        if (!history.length && !docs.length) {
          activeChat.project_id = target;
          return Object.assign({ started: false }, chatsView(true));
        }
        chatId += 1;
        const chat = { id: chatId, project_id: target, title: '', updated: nowTs(), history: [], docs: [] };
        chats.push(chat);
        openDemoChat(chat);
        return Object.assign({ started: true }, chatsView(true));
      },
      async chatOpen(id) {
        await sleep(200);
        const chat = chats.find((c) => c.id === id);
        if (!chat) throw new ApiError('not_found', 'Этого чата больше нет.', 404);
        openDemoChat(chat);
        return chatsView(true);
      },
      async chatRename(id, title) {
        await sleep(150);
        const chat = chats.find((c) => c.id === id);
        if (chat) chat.title = title;
        return chatsView();
      },
      async chatDelete(id) {
        await sleep(200);
        chats = chats.filter((c) => c.id !== id);
        if (activeChat.id === id) {
          const next = chats.slice().sort((a, b) => b.updated - a.updated)[0];
          if (next) openDemoChat(next);
          else {
            chatId += 1;
            const chat = { id: chatId, project_id: null, title: '', updated: nowTs(), history: [], docs: [] };
            chats.push(chat);
            openDemoChat(chat);
          }
        }
        return chatsView(true);
      },
      async projectSave(data) {
        await sleep(250);
        let project;
        if (data.id) {
          project = projects.find((p) => p.id === data.id);
          Object.assign(project, { name: data.name.trim(), emoji: data.emoji, instructions: data.instructions.trim(), mode: data.mode });
        } else {
          projectId += 1;
          project = { id: projectId, name: data.name.trim(), emoji: data.emoji, instructions: data.instructions.trim(), mode: data.mode };
          projects.push(project);
          if (data.open) await this.newChat(project.id);
        }
        return Object.assign({ project: Object.assign({}, project) }, chatsView(Boolean(data.open)));
      },
      async projectDelete(id) {
        await sleep(200);
        projects = projects.filter((p) => p.id !== id);
        chats.forEach((chat) => {
          if (chat.project_id === id) chat.project_id = null;
        });
        return chatsView();
      },
      async projectFiles(id) {
        await sleep(150);
        return { files: (projectFiles[id] || []).slice() };
      },
      async projectFileUpload(id, file) {
        await sleep(700);
        docId += 1;
        (projectFiles[id] = projectFiles[id] || []).push({ id: docId, name: file.name, kind: 'pdf', info: 'PDF, 12 стр.', project_id: id });
        return { files: projectFiles[id].slice(), documents: docs.slice() };
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
    const labels = {
      online: state.mode === 'code' ? 'режим кода' : 'на связи',
      typing: state.video.working ? 'монтирую…' : state.mode === 'code' ? 'пишет код…' : 'печатает…',
      offline: 'нет связи',
    };
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

  function artImage(src) {
    const image = el('img', 'bubble__art');
    image.alt = 'Нарисованная картинка';
    image.decoding = 'async';
    // Картинка подгружается после пузыря: лента досматривается до конца, если была внизу.
    image.addEventListener('load', () => {
      if (state.stick && !userIsScrolling()) scrollToBottom(false);
    });
    image.src = src;
    return image;
  }

  function againChip(id) {
    const button = el('button', 'chip-btn');
    button.type = 'button';
    button.append(icon('i-retry'), el('span', '', 'Ещё вариант'));
    button.addEventListener('click', () => drawAgain(id));
    return button;
  }

  /* «Изменить»: следующая просьба («сделай фон синим») правит эту картинку. */
  function editChip(id) {
    const button = el('button', 'chip-btn');
    button.type = 'button';
    button.append(el('span', '', '✏️ Изменить'));
    button.addEventListener('click', () => selectForEdit(id));
    return button;
  }

  async function selectForEdit(id) {
    try {
      await backend.drawSelect(id);
    } catch (error) {
      showToast(error.message);
      return;
    }
    dom.input.placeholder = 'Что изменить на картинке? Например: сделай фон синим';
    haptic('select');
    dom.input.focus();
  }

  function addMessage(message, options) {
    const fresh = Boolean(options && options.fresh);
    addDay(message.ts, fresh);
    if (message.role === 'break') {
      // Разделитель «Новый чат»: выше — прошлая переписка, Эля её уже не учитывает.
      const mark = el('div', 'chat-break' + (fresh ? ' chat-break--new' : ''));
      mark.setAttribute('role', 'separator');
      mark.append(el('span', '', 'Новый чат'));
      dom.messages.append(mark);
      return null;
    }

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
      if (message.image) {
        // Нарисованная картинка: сама картинка, под ней подпись и промпт.
        bubble.classList.add('bubble--art');
        bubble.append(artImage(message.image));
        if (message.content) bubble.append(window.ElyaMarkdown.render(message.content));
        if (message.sent_to_chat) bubble.append(el('p', 'bubble__note', 'Картинка есть и в чате с ботом — там её можно сохранить.'));
        if (message.drawing_id) meta.append(againChip(message.drawing_id));
        if (message.drawing_id && state.images && state.images.edit) meta.append(editChip(message.drawing_id));
      } else {
        if (message.speed) meta.append(el('span', 'msg__speed', message.speed));
        meta.append(copyChip(message.content, 'Копировать'));
        if (!(options && options.reveal)) bubble.append(window.ElyaMarkdown.render(message.content));
      }
      if (message.files && message.files.length) root.append(fileCards(message.files));
      if (message.push) root.append(pushButton(message.push));
    } else if (options && options.file) {
      bubble.append(el('span', 'bubble__file', options.file.icon + ' ' + options.file.name));
      if (message.content) bubble.append(el('span', 'bubble__caption', message.content));
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
    if (reply.image) {
      addMessage(reply, { fresh: true });
      return;
    }
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
    if (reply.speed) meta.insertBefore(el('span', 'msg__speed', reply.speed), copy);
    live.view.end(reply.content);
    if (reply.push) live.item.root.insertBefore(pushButton(reply.push), meta);
    if (reply.files && reply.files.length) {
      live.item.root.insertBefore(fileCards(reply.files), meta);
      if (state.stick) scrollToBottom(true);
    }
  }

  /* ---------- GitHub ---------- */

  function openExternal(url) {
    if (IN_TELEGRAM && typeof tg.openLink === 'function') tg.openLink(url);
    else window.open(url, '_blank', 'noopener');
  }

  /* «Отправить в GitHub» под ответом с файлами. */
  function pushButton(push) {
    const button = el('button', 'push-btn ripple');
    button.type = 'button';
    const count = push.files + ' ' + plural(push.files, ['файл', 'файла', 'файлов']);
    const label = el('span', 'push-btn__label', 'Отправить в GitHub · ' + count + (push.repo ? ' → ' + push.repo : ''));
    button.append(el('span', 'push-btn__icon', '🐙'), label);
    button.addEventListener('click', async () => {
      if (button.classList.contains('is-done')) {
        openExternal(button.dataset.url);
        return;
      }
      button.disabled = true;
      button.classList.add('is-loading');
      try {
        const data = await backend.github({ action: 'push', id: push.id });
        state.github = data.github;
        button.classList.add('is-done');
        button.dataset.url = data.pushed.commit_url;
        label.textContent = '✅ В ' + data.pushed.repo + ' · открыть коммит';
        haptic('notify', 'success');
      } catch (error) {
        showToast(error.message);
        haptic('notify', 'error');
      } finally {
        button.disabled = false;
        button.classList.remove('is-loading');
      }
    });
    return button;
  }

  function renderGitHub() {
    const gh = state.github;
    dom.githubBlock.hidden = gh === null;
    if (!gh) return;
    dom.githubConnect.hidden = gh.connected;
    dom.githubRepo.hidden = !gh.connected;
    dom.githubDisconnect.hidden = gh.from_env;
    dom.githubStatus.textContent = !gh.connected ? 'Не подключён'
      : '✅ ' + (gh.login ? gh.login : 'Подключён') + (gh.repo ? ' · ' + gh.repo + ' (' + (gh.branch || 'main') + ')' : ' · выбери репозиторий');
    const repos = state.githubRepos || (gh.repo ? [gh.repo] : []);
    dom.githubRepos.replaceChildren(...repos.map((name) => {
      const button = el('button', 'repo-chip ripple' + (name === gh.repo ? ' is-on' : ''), name);
      button.type = 'button';
      button.dataset.repo = name;
      return button;
    }));
    if (gh.connected && state.githubRepos === null) loadRepos();
  }

  async function loadRepos() {
    state.githubRepos = [];
    try {
      state.githubRepos = (await backend.github({ action: 'repos' })).repos || [];
    } catch (error) {
      showToast(error.message);
    }
    renderGitHub();
  }

  async function githubAction(body, success) {
    try {
      const data = await backend.github(body);
      state.github = data.github;
      if (body.action !== 'use') state.githubRepos = null;
      renderGitHub();
      if (success) showToast(success);
      haptic('notify', 'success');
      return true;
    } catch (error) {
      showToast(error.message);
      haptic('notify', 'error');
      return false;
    }
  }

  function bindGitHub() {
    dom.githubConnectBtn.addEventListener('click', async () => {
      const token = dom.githubToken.value.trim();
      if (!token) return;
      dom.githubConnectBtn.disabled = true;
      if (await githubAction({ action: 'connect', token }, '🐙 GitHub подключён')) dom.githubToken.value = '';
      dom.githubConnectBtn.disabled = false;
    });
    dom.githubRepos.addEventListener('click', (event) => {
      const chip = event.target.closest('[data-repo]');
      if (chip) githubAction({ action: 'use', repo: chip.dataset.repo }, 'Работаем с ' + chip.dataset.repo);
    });
    dom.githubCreate.addEventListener('click', async () => {
      const name = dom.githubNewName.value.trim();
      if (!name) return;
      dom.githubCreate.disabled = true;
      if (await githubAction({ action: 'create', name }, 'Репозиторий создан')) dom.githubNewName.value = '';
      dom.githubCreate.disabled = false;
    });
    dom.githubDisconnect.addEventListener('click', () => githubAction({ action: 'disconnect' }, 'GitHub отключён'));
  }

  /* ---------- Готовые файлы: DOCX, PPTX, ZIP, HTML ---------- */

  const FILE_ICONS = { docx: '📄', pptx: '🎞', zip: '🗂', html: '🌐', image: '📈', data: '📎' };
  const FILE_KINDS = {
    docx: 'Документ Word', pptx: 'Презентация', zip: 'Архив проекта', html: 'Веб-страница',
    image: 'График', data: 'Файл из песочницы',
  };

  function fileUrl(file) {
    return new URL('files/' + encodeURIComponent(file.token) + '/' + encodeURIComponent(file.name), window.location.href).href;
  }

  // Картинка-заглушка для демо-режима: синусоида.
  const DEMO_CHART = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180"><rect width="320" height="180" fill="#fff"/>' +
    '<path d="M20 90 H300 M20 20 V160" stroke="#bbb"/><path d="' +
    Array.from({ length: 57 }, (_, i) => (i ? 'L' : 'M') + (20 + i * 5) + ' ' + (90 - 55 * Math.sin(i * Math.PI / 28)).toFixed(1)).join(' ') +
    '" fill="none" stroke="#1f77b4" stroke-width="2.5"/><text x="160" y="16" font-size="13" text-anchor="middle" font-family="sans-serif">Синус</text></svg>'
  );

  /* График из песочницы — сразу картинкой; по нажатию открывается целиком. */
  function imageCard(file, index) {
    const figure = el('button', 'file-image ripple');
    figure.type = 'button';
    figure.style.setProperty('--i', index);
    figure.setAttribute('aria-label', 'Открыть ' + file.name);
    const img = el('img', 'file-image__img');
    img.alt = file.name;
    img.loading = 'lazy';
    img.src = DEMO ? DEMO_CHART : fileUrl(file);
    img.addEventListener('load', () => {
      if (state.stick) scrollToBottom(true);
    });
    figure.append(img, el('span', 'file-image__name', '📈 ' + file.name));
    figure.addEventListener('click', () => downloadFile(file));
    return figure;
  }

  function fileCards(files) {
    const box = el('div', 'file-cards');
    files.forEach((file, index) => {
      if (file.kind === 'image') {
        box.append(imageCard(file, index));
        return;
      }
      const card = el('div', 'file-card');
      card.style.setProperty('--i', index);
      card.append(el('span', 'file-card__icon', FILE_ICONS[file.kind] || '📎'));
      const text = el('span', 'file-card__text');
      text.append(
        el('span', 'file-card__name', file.name),
        el('span', 'file-card__meta', (FILE_KINDS[file.kind] || 'Файл') + (file.size ? ' · ' + formatSize(file.size) : ''))
      );
      card.append(text);
      const actions = el('span', 'file-card__actions');
      if (file.preview) {
        const look = el('button', 'file-card__btn file-card__btn--main ripple', '👁 Открыть');
        look.type = 'button';
        look.addEventListener('click', () => openPreview(file));
        actions.append(look);
      }
      const save = el('button', 'file-card__btn ripple' + (file.preview ? '' : ' file-card__btn--main'), 'Скачать');
      save.type = 'button';
      save.addEventListener('click', () => downloadFile(file));
      actions.append(save);
      card.append(actions);
      box.append(card);
    });
    return box;
  }

  function downloadFile(file) {
    if (DEMO) {
      showToast('В демо-режиме файлы не скачиваются — в Telegram придёт настоящий ' + file.name);
      return;
    }
    const url = fileUrl(file);
    haptic('impact', 'light');
    try {
      if (supports('8.0') && typeof tg.downloadFile === 'function') {
        tg.downloadFile({ url, file_name: file.name });
        return;
      }
      if (IN_TELEGRAM && typeof tg.openLink === 'function') {
        tg.openLink(url);
        return;
      }
    } catch (error) { /* старый клиент — откроем ссылку как есть */ }
    window.open(url, '_blank', 'noopener');
  }

  /* Предпросмотр HTML: страница в изолированной «песочнице» поверх чата. */
  function openPreview(file) {
    closePreview(true);
    const layer = el('div', 'preview');
    layer.setAttribute('role', 'dialog');
    layer.setAttribute('aria-label', 'Предпросмотр: ' + file.name);
    const head = el('div', 'preview__head');
    const title = el('span', 'preview__title', '🌐 ' + file.name);
    const save = el('button', 'preview__btn ripple', 'Скачать');
    save.type = 'button';
    save.addEventListener('click', () => downloadFile(file));
    const close = el('button', 'icon-btn icon-btn--soft ripple preview__close');
    close.type = 'button';
    close.setAttribute('aria-label', 'Закрыть предпросмотр');
    close.append(icon('i-close'));
    close.addEventListener('click', () => closePreview());
    head.append(title, save, close);
    const frame = el('iframe', 'preview__frame');
    frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-modals allow-popups allow-pointer-lock');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.title = file.name;
    frame.src = DEMO ? 'about:blank' : new URL('preview/' + encodeURIComponent(file.token), window.location.href).href;
    if (DEMO) frame.srcdoc = '<p style="font:16px sans-serif;padding:24px">Здесь откроется страница из ответа Эли.</p>';
    layer.append(head, frame);
    document.body.append(layer);
    state.preview = layer;
    motion(layer, [{ opacity: 0, transform: 'translateY(24px) scale(0.98)' }, { opacity: 1, transform: 'none' }],
      { duration: 360, easing: 'cubic-bezier(0.2, 1, 0.3, 1)' });
    if (supports('6.1')) tg.BackButton.show();
    haptic('impact', 'light');
  }

  function closePreview(instant) {
    const layer = state.preview;
    if (!layer) return false;
    state.preview = null;
    if (supports('6.1') && dom.sheet.hidden && dom.drawer.hidden) tg.BackButton.hide();
    if (instant || reducedMotion) {
      layer.remove();
    } else {
      const out = motion(layer, [{ opacity: 1 }, { opacity: 0, transform: 'translateY(24px)' }], { duration: 200, fill: 'forwards' });
      if (out) out.onfinish = () => layer.remove();
      else layer.remove();
    }
    return true;
  }

  /* Кнопка «Посмотреть в приложении» под HTML в чате с ботом открывает приложение с ?preview=. */
  async function openPreviewFromLink() {
    const token = query.get('preview');
    if (!token || DEMO) return;
    try {
      const data = await backend.fileInfo(token);
      openPreview(data.file);
    } catch (error) {
      showToast(error.message);
    }
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
    // Объёмная звёздочка: свечение + три слоя на разной глубине (см. .typing__spark в CSS).
    const spark = el('span', 'typing__spark');
    spark.setAttribute('aria-hidden', 'true');
    const body = el('span', 'spark-body');
    body.append(el('span', 'spark spark--back'), el('span', 'spark spark--mid'), el('span', 'spark spark--front'));
    spark.append(el('span', 'spark-glow'), body);
    bubble.append(spark);
    bubble.setAttribute('aria-label', state.assistantName + ' печатает');
    root.append(bubble);
    dom.messages.append(root);

    const setHint = (text) => {
      const from = bubble.offsetWidth;
      let hint = bubble.querySelector('.typing__hint');
      if (!hint) {
        hint = el('span', 'typing__hint');
        bubble.append(hint);
      }
      hint.textContent = text;
      motion(hint, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 350, easing: 'ease-out' });
      motion(bubble, [{ width: from + 'px' }, { width: bubble.offsetWidth + 'px' }], { duration: 450, easing: SPRING });
    };
    // Если модель долго думает, рядом со звёздочкой появляется пояснение.
    let timers = TYPING_HINTS.map(([delay, text]) => setTimeout(() => setHint(text), delay));
    /* Эля ищет в интернете: показываем запрос, дальше — обычные подсказки заново. */
    root.searching = (query) => {
      timers.forEach(clearTimeout);
      root.classList.add('typing--search');
      setHint(query ? 'ищу в интернете: «' + query + '»…' : 'ищу в интернете…');
      timers = TYPING_HINTS.slice(1).map(([delay, text]) => setTimeout(() => setHint(text), delay + 4000));
    };
    /* Долгая работа (презентация): что сейчас делается. */
    root.status = (text) => {
      timers.forEach(clearTimeout);
      timers = [];
      setHint(text);
    };
    /* Документ загружается и читается — до ответа может пройти пара секунд. */
    root.reading = () => {
      timers.forEach(clearTimeout);
      setHint('читаю документ…');
      timers = TYPING_HINTS.slice(1).map(([delay, text]) => setTimeout(() => setHint(text), delay + 3000));
    };
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
    dom.composerBox.classList.toggle('can-attach', state.vision || state.video.available || state.documents !== null);
    dom.attach.disabled = state.busy;
    dom.composerBox.classList.toggle('can-search', state.search && !state.attachment);
    dom.searchToggle.disabled = state.busy;
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

  /* Кнопка 🌐: следующее сообщение Эля обязательно проверит в интернете. */
  function setSearchNext(on) {
    const value = Boolean(on) && state.search;
    state.searchNext = value;
    dom.searchToggle.classList.toggle('is-on', value);
    dom.searchToggle.setAttribute('aria-pressed', String(value));
    if (!state.attachment) dom.input.placeholder = inputPlaceholder();
  }

  async function send(rawText, failedItem, origin, attachment, search) {
    const text = String(rawText).trim();
    if (!failedItem && !attachment && !state.busy && text) {
      // /video и всё, что пишут во время монтажа, — монтажёру, а не в обычный чат.
      const command = text.toLowerCase();
      if (/^\/video(@\w+)?(\s|$)/.test(command)) {
        startVideo();
        return;
      }
      if (state.video.session) {
        if (/^\/(cancel|done)(@\w+)?$/.test(command)) finishVideo();
        else sendVideo(text, null);
        return;
      }
    }
    if ((!text && !attachment) || state.busy) return;
    // Повтор после ошибки ищет так же, как исходное сообщение.
    const searchOn = search !== undefined ? search : state.searchNext && !attachment;
    if (search === undefined && state.searchNext) setSearchNext(false);

    if (state.finishReveal) state.finishReveal();
    clearNotice();
    setBusy(true);
    haptic('impact', 'light');

    const fly = Boolean(origin) && !attachment && !failedItem && !reducedMotion && typeof Element.prototype.animate === 'function';
    let item = failedItem;
    if (item) {
      item.root.classList.remove('msg--failed');
    } else {
      const doc = attachment && attachment.kind === 'document';
      item = addMessage(
        { role: 'user', content: text, ts: nowTs() },
        {
          fresh: true,
          flight: fly,
          image: attachment && !doc ? attachment.url : null,
          file: doc ? { icon: '📄', name: attachment.name } : null,
        }
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

    const stream = (documents) => backend.chatStream(text, state.model, (piece) => {
      // Первый кусок ответа — убираем «печатает» и открываем пузырь.
      if (!live) {
        typing.dismiss();
        live = startStreamingReply();
      }
      live.text += piece;
      live.view.push(live.text);
    }, {
      search: searchOn,
      documents,
      onSearch: (query) => {
        if (!live) typing.searching(query);
      },
      onStatus: (text) => {
        if (!live) typing.status(text);
      },
      // Эля запустила код: до результата под текстом видно, что он выполняется.
      onRun: () => {
        if (!live) {
          typing.status('запускаю код…');
          return;
        }
        live.view.push(live.text + '\n\n⏳ *Запускаю код…*');
      },
    });

    try {
      let data;
      if (attachment && attachment.kind === 'document') {
        // Сначала файл уходит на сервер (один раз, даже если потом ответ повторяют),
        // затем вопрос к нему — обычным сообщением.
        if (!attachment.docId) {
          typing.reading();
          const uploaded = await backend.documentUpload(attachment.file, () => {});
          attachment.docId = uploaded.document.id;
          setDocuments(uploaded.documents);
          if (uploaded.truncated) showToast('Документ очень большой: я прочитала только его начало.');
        }
        data = await stream([attachment.docId]);
      } else if (attachment) {
        data = await backend.photo(text, attachment.blob, state.model);
      } else {
        data = await stream();
      }
      typing.dismiss();
      if (live) finishStreamingReply(live, data.reply);
      else addAssistantReply(data.reply);
      haptic('impact', 'soft');
      if (data.usage) state.usage = data.usage;
      watchNewMemories();
      noteChatActivity(text || (attachment && attachment.kind === 'document' ? attachment.name : '📷 Фото'));
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
      showNotice(error.message, () => send(text, item, null, attachment, searchOn));
      scrollToBottom(true);
    }
  }

  /* «Ещё вариант» под картинкой: в ленте — как сообщение пользователя, затем новая картинка. */
  async function drawAgain(id) {
    if (state.busy) return;
    if (state.finishReveal) state.finishReveal();
    clearNotice();
    setBusy(true);
    haptic('impact', 'light');
    const item = addMessage({ role: 'user', content: '🔄 Ещё вариант', ts: nowTs() }, { fresh: true });
    const typing = showTyping();
    scrollToBottom(true);
    try {
      const data = await backend.drawAgain(id);
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
      showNotice(error.message, () => {
        item.root.remove();
        drawAgain(id);
      });
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
    state.attachment = { kind: 'photo', blob, url: URL.createObjectURL(blob) };
    dom.attachmentImg.src = state.attachment.url;
    dom.attachmentImg.hidden = false;
    dom.attachmentFile.hidden = true;
    dom.attachment.classList.remove('is-leaving');
    dom.attachment.hidden = false;
    if (state.searchNext) setSearchNext(false);
    dom.input.placeholder = state.images && state.images.edit
      ? 'Вопрос к фото или что изменить…'
      : 'Подпись или вопрос к фото…';
    haptic('impact', 'light');
    updateComposer();
    if (finePointer) dom.input.focus();
  }

  /* keepUrl — ссылка ещё нужна пузырю сообщения, в котором показано фото. */
  function clearAttachment(instant, keepUrl) {
    const current = state.attachment;
    state.attachment = null;
    dom.input.placeholder = inputPlaceholder();
    if (current && current.url && !keepUrl) URL.revokeObjectURL(current.url);
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

  /* ---------- Монтаж видео ---------- */

  function formatSize(bytes) {
    const mb = bytes / 1024 / 1024;
    if (mb >= 1) return (mb >= 10 ? Math.round(mb) : mb.toFixed(1).replace('.', ',')) + ' МБ';
    return Math.max(1, Math.round(bytes / 1024)) + ' КБ';
  }

  function applyVideo(info) {
    const data = info || {};
    state.video.available = Boolean(data.available);
    if (data.upload_limit_mb) state.video.uploadLimitMb = data.upload_limit_mb;
    setVideoSession(state.video.available ? data.session : null);
  }

  function setVideoSession(session) {
    state.video.session = session || null;
    renderVideoBar();
  }

  /* Плашка над полем ввода, пока идёт монтаж: сколько клипов и кнопка «Готово». */
  function renderVideoBar() {
    const session = state.video.session;
    dom.videoBar.hidden = !session;
    if (session) {
      const count = session.clips.length;
      let text = 'Монтаж';
      text += count ? ' · ' + count + ' ' + plural(count, ['клип', 'клипа', 'клипов']) : ' · жду видео';
      if (session.music) text += ' · 🎵';
      dom.videoBarText.textContent = text;
    }
    const accept = [];
    if (state.vision) accept.push('image/*');
    if (state.video.available) accept.push('video/*', 'audio/*');
    if (state.documents !== null) accept.push(DOCUMENT_ACCEPT);
    dom.fileInput.accept = accept.join(',') || 'image/*';
    if (!state.attachment) dom.input.placeholder = inputPlaceholder();
    updateComposer();
  }

  function pickFile(file) {
    if (!file) return;
    const name = file.name || '';
    if (DOCUMENT_RE.test(name)) pickDocument(file);
    else if (/\.(doc|xls|rtf|odt|pptx?)$/i.test(name)) {
      showToast('Такой формат я не читаю: сохрани файл как PDF, .docx или .xlsx.');
    } else if (/^(video|audio)\//.test(file.type)) pickMedia(file);
    else pickPhoto(file);
  }

  /* ---------- Документы ---------- */

  const DOCUMENT_RE = /\.(pdf|docx|xlsx|xlsm|csv|txt|md)$/i;
  const DOCUMENT_ACCEPT = '.pdf,.docx,.xlsx,.xlsm,.csv,.txt,.md,application/pdf';
  const MAX_DOCUMENT_MB = 20;

  function pickDocument(file) {
    if (state.documents === null) {
      showToast('Документы сейчас недоступны: сервер их не читает.');
      return;
    }
    if (file.size > MAX_DOCUMENT_MB * 1024 * 1024) {
      showToast('Файл больше ' + MAX_DOCUMENT_MB + ' МБ — пришли часть документа или сожми PDF.');
      return;
    }
    clearAttachment(true);
    state.attachment = { kind: 'document', file, name: file.name || 'Документ' };
    dom.attachmentImg.hidden = true;
    dom.attachmentFile.hidden = false;
    dom.attachmentIcon.textContent = '📄';
    dom.attachmentName.textContent = state.attachment.name;
    dom.attachmentSize.textContent = formatSize(file.size);
    dom.attachment.classList.remove('is-leaving');
    dom.attachment.hidden = false;
    if (state.searchNext) setSearchNext(false);
    dom.input.placeholder = 'Что найти в документе?';
    haptic('impact', 'light');
    updateComposer();
    if (finePointer) dom.input.focus();
  }

  /* Плашка над полем ввода: документы разговора, лишний можно убрать. */
  function setDocuments(list) {
    if (state.documents === null && list == null) return;
    state.documents = Array.isArray(list) ? list : null;
    renderDocuments();
  }

  function renderDocuments() {
    const docs = state.documents || [];
    dom.docsList.textContent = '';
    docs.forEach((doc) => {
      const chip = el('span', 'docs-chip');
      chip.title = doc.name + ' — ' + doc.info;
      chip.append(el('span', 'docs-chip__icon', doc.project_id ? '🗂' : '📄'), el('span', 'docs-chip__name', doc.name));
      if (doc.project_id) chip.title += ' (файл проекта)';
      const remove = el('button', 'docs-chip__remove ripple');
      remove.type = 'button';
      remove.setAttribute('aria-label', 'Убрать документ «' + doc.name + '»');
      remove.append(icon('i-close'));
      remove.addEventListener('click', () => removeDocument(doc.id));
      chip.append(remove);
      dom.docsList.append(chip);
    });
    dom.docsBar.hidden = !docs.length;
    renderVideoBar();
  }

  async function removeDocument(id) {
    try {
      const data = await backend.documentsDelete(id);
      setDocuments(data.documents);
      haptic('selection');
    } catch (error) {
      showToast(error.message);
    }
  }

  function pickMedia(file) {
    if (!state.video.available) {
      showToast('Монтаж видео сейчас недоступен: на сервере нет ffmpeg.');
      return;
    }
    if (file.size > state.video.uploadLimitMb * 1024 * 1024) {
      showToast('Файл больше ' + state.video.uploadLimitMb + ' МБ — выбери покороче.');
      return;
    }
    const music = /^audio\//.test(file.type);
    clearAttachment(true);
    state.attachment = { kind: music ? 'music' : 'video', file, name: file.name || (music ? 'Музыка' : 'Видео') };
    dom.attachmentImg.hidden = true;
    dom.attachmentFile.hidden = false;
    dom.attachmentIcon.textContent = music ? '🎵' : '🎬';
    dom.attachmentName.textContent = state.attachment.name;
    dom.attachmentSize.textContent = formatSize(file.size);
    dom.attachment.classList.remove('is-leaving');
    dom.attachment.hidden = false;
    dom.input.placeholder = music ? 'Что сделать с музыкой? Можно не писать' : 'Что сделать с видео? Можно не писать';
    haptic('impact', 'light');
    updateComposer();
    if (finePointer) dom.input.focus();
  }

  /* Пузырь Эли, в котором виден ход монтажа; потом в нём же результат. */
  function addVideoBubble(text) {
    addDay(nowTs(), true);
    const root = el('article', 'msg msg--assistant msg--new msg--video');
    const bubble = el('div', 'bubble');
    const status = el('p', 'video-status');
    const label = el('span', 'video-status__text', text);
    status.append(el('span', 'video-status__spinner'), label);
    bubble.append(status);
    root.append(bubble);
    dom.messages.append(root);
    scrollToBottom(true);
    return {
      root,
      bubble,
      set(next) {
        label.textContent = next;
        if (state.stick && !userIsScrolling()) scrollToBottom(false);
      },
    };
  }

  function videoPreview(outcome) {
    const audio = outcome.kind === 'audio';
    const box = el('button', 'video-preview' + (audio ? ' video-preview--audio' : '') +
      (outcome.kind === 'video_note' ? ' video-preview--circle' : ''));
    box.type = 'button';
    box.setAttribute('aria-label', audio ? 'Послушать' : 'Посмотреть видео');
    if (outcome.poster) {
      const poster = el('img', 'video-preview__poster');
      poster.src = outcome.poster;
      poster.alt = '';
      box.append(poster);
    }
    if (!audio && outcome.width && outcome.height) {
      box.style.aspectRatio = outcome.width + ' / ' + outcome.height;
      if (outcome.height > outcome.width * 1.1) box.classList.add('video-preview--tall');
    }
    const play = el('span', 'video-preview__play');
    play.append(icon('i-play'));
    box.append(play);
    if (audio) box.append(el('span', 'video-preview__label', 'Послушать'));
    box.addEventListener('click', () => playResult(box, outcome));
    return box;
  }

  async function playResult(box, outcome) {
    if (box.classList.contains('is-loading')) return;
    box.classList.add('is-loading');
    try {
      const blob = await backend.videoResult(outcome.result);
      const player = el(outcome.kind === 'audio' ? 'audio' : 'video', 'video-player');
      player.src = URL.createObjectURL(blob);
      player.controls = true;
      player.autoplay = true;
      player.setAttribute('playsinline', '');
      if (outcome.kind === 'animation') {
        player.loop = true;
        player.muted = true;
      }
      if (box.style.aspectRatio) player.style.aspectRatio = box.style.aspectRatio;
      ['video-preview--tall', 'video-preview--circle'].forEach((name) => {
        if (box.classList.contains(name)) player.classList.add(name);
      });
      box.replaceWith(player);
    } catch (error) {
      box.classList.remove('is-loading');
      showToast(error.message);
    }
  }

  function hideVideoActions() {
    dom.messages.querySelectorAll('.video-actions').forEach((node) => node.remove());
  }

  /* Кнопки быстрых правок — только под последним ответом, как в чате с ботом. */
  function showVideoActions(root) {
    hideVideoActions();
    const session = state.video.session;
    if (!session || !session.actions || !session.actions.length || !session.clips.length) return;
    const row = el('div', 'video-actions');
    session.actions.forEach((item) => {
      const button = el('button', 'video-action ripple', item.label);
      button.type = 'button';
      button.dataset.videoAction = item.action;
      button.dataset.value = item.value;
      row.append(button);
    });
    const meta = root.querySelector('.msg__meta');
    if (meta) root.insertBefore(row, meta);
    else root.append(row);
  }

  function finishVideoBubble(view, text, outcome) {
    view.bubble.replaceChildren(window.ElyaMarkdown.render(text));
    if (outcome && outcome.error) view.root.classList.add('msg--video-error');
    if (outcome && outcome.result) view.bubble.append(videoPreview(outcome));
    const meta = el('div', 'msg__meta');
    const ts = nowTs();
    const time = el('time', '', timeFormat.format(new Date(ts * 1000)));
    time.dateTime = new Date(ts * 1000).toISOString();
    meta.append(time, copyChip(text, 'Копировать'));
    view.root.append(meta);
    showVideoActions(view.root);
    if (state.stick) scrollToBottom(true);
  }

  /* Общая обвязка: сообщение пользователя, пузырь с ходом работы, результат или ошибка с повтором. */
  async function videoOp(userText, run, options) {
    if (state.busy) return;
    const opts = options || {};
    if (state.finishReveal) state.finishReveal();
    clearNotice();
    state.video.working = true;
    setBusy(true);
    haptic('impact', 'light');
    let item = opts.item || null;
    if (item) item.root.classList.remove('msg--failed');
    else if (userText || opts.file) {
      item = addMessage({ role: 'user', content: userText, ts: nowTs() }, { fresh: true, file: opts.file || null });
    }
    updateWelcome();
    hideVideoActions();
    const view = addVideoBubble(opts.first || '⏳ Монтирую…');
    try {
      const data = await run(view);
      if ('session' in data) setVideoSession(data.session);
      finishVideoBubble(view, data.outcome ? data.outcome.text : data.message, data.outcome);
      haptic('impact', 'soft');
      if (data.outcome && data.outcome.error) haptic('notify', 'warning');
    } catch (error) {
      view.root.remove();
      haptic('notify', 'error');
      if (error.status === 401 || error.status === 403) {
        state.video.working = false;
        setBusy(false);
        showLoadError(error, boot);
        return;
      }
      if (error.code === 'no_session') setVideoSession(null);
      if (error.code === 'network') setStatus('offline');
      if (item) item.root.classList.add('msg--failed');
      showNotice(error.message, () => videoOp(userText, run, Object.assign({}, opts, { item })));
      scrollToBottom(true);
    } finally {
      state.video.working = false;
      if (state.busy) setBusy(false);
    }
  }

  function sendVideo(text, attachment) {
    let uploaded = !attachment;
    const music = attachment && attachment.kind === 'music';
    const what = music ? 'музыку' : 'видео';
    const run = async (view) => {
      if (!uploaded) {
        view.set('⏳ Загружаю ' + what + '…');
        const data = await backend.videoUpload(attachment.file, (pct) => view.set('⏳ Загружаю ' + what + ': ' + pct + '%'));
        uploaded = true; // при повторе после ошибки файл заново не грузим
        setVideoSession(data.session);
        if (!text) return data;
      }
      view.set('🤔 Думаю, как смонтировать…');
      return backend.videoEdit(text, (step) => view.set(step));
    };
    videoOp(text, run, {
      file: attachment ? { icon: music ? '🎵' : '🎬', name: attachment.name } : null,
      first: attachment ? '⏳ Загружаю ' + what + '…' : '🤔 Думаю, как смонтировать…',
    });
  }

  function startVideo() {
    videoOp('/video', () => backend.videoStart(), { first: '🎬 Готовлю монтаж…' });
  }

  function finishVideo() {
    if (!state.video.session || state.busy) return;
    videoOp('Готово', () => backend.videoAction('done'), { first: 'Заканчиваю монтаж…' });
  }

  function runVideoAction(action, value, label) {
    if (state.busy) return;
    videoOp(label, (view) => backend.videoAction(action, value, (step) => view.set(step)), {
      first: action === 'undo' ? '↩️ Возвращаю прошлый вариант…' : '⏳ Монтирую…',
    });
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
      if (attachment.kind === 'photo' || attachment.kind === 'document') send(text, null, null, attachment);
      else sendVideo(text.trim(), attachment);
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

  /* ---------- Режим кода ---------- */

  function inputPlaceholder() {
    if (state.video.session) return 'Что сделать с видео?';
    if (state.searchNext) return 'Что найти в интернете?';
    const base = state.mode === 'code' ? 'Опиши задачу или вставь код…' : 'Напиши сообщение…';
    const project = currentProject();
    return project ? project.emoji + ' ' + project.name + ': ' + base.charAt(0).toLowerCase() + base.slice(1) : base;
  }

  function renderMode() {
    const on = state.mode === 'code';
    dom.codeBlock.hidden = state.mode === null;
    dom.codeToggle.setAttribute('aria-checked', String(on));
    dom.codeSub.textContent = on ? 'Включён: отвечаю как программист' : 'Выключен';
    dom.input.maxLength = state.maxLength;
    if (!state.attachment) dom.input.placeholder = inputPlaceholder();
    if (!state.busy) setStatus(dom.status.dataset.state === 'offline' ? 'offline' : 'online');
    updateComposer();
  }

  /* Ответ сервера: режим и лимит длины сообщения, который от него зависит. */
  function takeMode(data) {
    state.mode = data.mode === undefined ? null : data.mode;
    if (data.limits && data.limits.max_text_length) state.maxLength = data.limits.max_text_length;
    renderMode();
  }

  async function toggleMode() {
    const previous = state.mode;
    const next = previous === 'code' ? 'chat' : 'code';
    state.mode = next;
    renderMode();
    haptic('select');
    try {
      const data = await backend.setMode(next);
      takeMode(data);
      if (data.locked) {
        const project = currentProject();
        showToast('В проекте «' + (project ? project.name : '') + '» режим кода включён всегда — это можно поменять в настройках проекта');
      } else {
        showToast(next === 'code' ? 'Режим кода включён' : 'Обычный режим');
      }
    } catch (error) {
      state.mode = previous;
      renderMode();
      showToast(error.message);
      haptic('notify', 'error');
    }
  }

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
    renderGitHub();
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
    const about = opts.page === 'about';
    const projectPage = opts.page === 'project';
    state.sheetPage = about ? 'about' : projectPage ? 'project' : 'main';
    dom.sheetBody.hidden = about || projectPage;
    dom.instructionsPage.hidden = true;
    dom.aboutPage.hidden = !about;
    dom.projectPage.hidden = !projectPage;
    dom.sheet.classList.toggle('is-deep', projectPage);
    dom.sheetBack.hidden = true;
    dom.sheetTitle.textContent = about ? 'Как я устроена?'
      : projectPage ? (opts.project ? opts.project.emoji + ' ' + opts.project.name : 'Новый проект')
      : 'Настройки';
    if (projectPage) {
      fillProjectForm(opts.project || null);
      dom.projectPage.scrollTop = 0;
      if (!opts.project && finePointer) setTimeout(() => dom.projectName.focus(), 400);
    }
    if (about) {
      dom.aboutPage.scrollTop = 0;
      setTimeout(renderAbout, reducedMotion ? 0 : 200);
    }
    dom.input.blur();

    state.sheetAnim = motion(dom.sheet, isWide()
      ? [{ opacity: 0, transform: 'perspective(1200px) translateY(18px) rotateX(16deg) scale(0.95)' }, { opacity: 1, transform: 'none' }]
      : [{ transform: 'perspective(1400px) translateY(100%) rotateX(24deg)' }, { transform: 'none' }],
    { duration: isWide() ? 420 : 600, easing: 'cubic-bezier(0.2, 1.08, 0.32, 1)' });
    motion(dom.backdrop, [{ opacity: 0 }, { opacity: 1 }], { duration: 300, easing: 'ease-out' });

    // Полоска и число заполняются уже на глазах.
    dom.usageFill.style.width = '0';
    dom.usageValue.textContent = '0';
    setTimeout(renderUsage, reducedMotion ? 0 : 180);
    if (supports('6.1')) tg.BackButton.show();
    haptic('impact', 'light');
    const focusBlock = { reminders: dom.remindersBlock, memory: dom.memoryBlock }[opts.focus];
    if (focusBlock) {
      // Колокольчик и шторка открывают настройки сразу на нужном разделе.
      setTimeout(() => {
        // offsetTop не зависит от анимации появления блоков (transform), в отличие от getBoundingClientRect.
        dom.sheetBody.scrollTo({ top: focusBlock.offsetTop - 8, behavior: reducedMotion ? 'auto' : 'smooth' });
      }, reducedMotion ? 0 : 380);
    } else {
      dom.sheetBody.scrollTop = 0;
    }
  }

  /* «Как я устроена?»: число строк кода считает сервер при запуске. */
  function renderAbout() {
    const total = state.codeLines;
    dom.aboutLinesLabel.textContent = plural(total, ['строка', 'строки', 'строк']) + ' кода';
    const format = (n) => n.toLocaleString('ru-RU');
    if (reducedMotion || total <= 0) {
      dom.aboutLines.textContent = total > 0 ? format(total) : '—';
      return;
    }
    const started = performance.now();
    const duration = 1100;
    (function step(now) {
      const t = Math.min(1, (now - started) / duration);
      dom.aboutLines.textContent = format(Math.round(total * (1 - Math.pow(1 - t, 3))));
      if (t < 1) requestAnimationFrame(step);
    })(started);
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
      ? [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'perspective(1200px) translateY(14px) rotateX(-14deg) scale(0.96)' }]
      : [{ transform: fromTransform }, { transform: 'perspective(1400px) translateY(100%) rotateX(24deg)' }],
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
      dom.aboutPage.hidden = true;
      dom.projectPage.hidden = true;
      state.projectEdit = null;
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
      applyChats(await backend.clear());
    } catch (error) {
      dom.clearYes.disabled = false;
      showToast(error.message);
      haptic('notify', 'error');
      return;
    }
    closeSheet();
    await wipeFeed('Чат очищен');
  }

  /* Сообщения разлетаются, лента начинается заново. */
  async function wipeFeed(toast) {
    const nodes = Array.from(dom.messages.children).slice(-14).reverse();
    nodes.forEach((node, index) => {
      node.style.setProperty('--i', index);
      node.classList.add('is-leaving');
    });
    if (nodes.length && !reducedMotion) await sleep(380 + nodes.length * 35);
    renderHistory([]);
    showToast(toast);
    haptic('notify', 'success');
  }

  /* ---------- Боковая шторка ---------- */

  function initials(name) {
    const words = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (!words.length) return state.assistantName.charAt(0).toUpperCase();
    return words.slice(0, 2).map((word) => word.charAt(0).toUpperCase()).join('');
  }

  function plural(n, forms) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return forms[0];
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
    return forms[2];
  }

  /* ---------- Чаты и проекты ---------- */

  const PROJECT_EMOJIS = ['📚', '💻', '🌿', '💼', '🎨', '🧪', '🌍', '🎵', '🏋️', '💡', '🏠', '✈️', '📁'];

  function currentProject() {
    if (!state.chat || state.chat.project_id == null) return null;
    return state.projects.find((project) => project.id === state.chat.project_id) || null;
  }

  function projectById(id) {
    return state.projects.find((project) => project.id === id) || null;
  }

  /* Ответ сервера с открытым чатом, списком чатов, проектами и документами. */
  function applyChats(data) {
    if (!data) return;
    if (data.chat) state.chat = data.chat;
    if (Array.isArray(data.chats)) state.chats = data.chats;
    if (Array.isArray(data.projects)) state.projects = data.projects;
    if (data.documents !== undefined) setDocuments(data.documents);
    if ('mode' in data) takeMode(data);
    if (state.chatFilter !== 'all' && !projectById(state.chatFilter)) state.chatFilter = 'all';
    renderChatHeader();
    if (!dom.drawer.hidden) renderChats();
  }

  /* После ответа: чат поднимается в списке и получает название (как на сервере). */
  function noteChatActivity(text) {
    const chat = state.chat;
    if (!chat) return;
    chat.updated = nowTs();
    if (!chat.title && text) {
      const line = String(text).split('\n').map((part) => part.trim()).find(Boolean) || '';
      chat.title = line.length > 60 ? line.slice(0, 59).trimEnd() + '…' : line;
    }
    state.chats = [Object.assign({}, chat)].concat(state.chats.filter((item) => item.id !== chat.id));
  }

  /* В шапке — проект открытого чата. */
  function renderChatHeader() {
    const project = currentProject();
    dom.topbarProject.hidden = !project;
    dom.topbarProject.textContent = project ? project.emoji + ' ' + project.name : '';
    if (!state.attachment) dom.input.placeholder = inputPlaceholder();
  }

  const chatDayFormat = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' });

  function chatWhen(ts) {
    const date = new Date(ts * 1000);
    const today = new Date();
    if (date.toDateString() === today.toDateString()) return timeFormat.format(date);
    const yesterday = new Date(today.getTime() - 86400000);
    if (date.toDateString() === yesterday.toDateString()) return 'вчера';
    return chatDayFormat.format(date).replace('.', '');
  }

  function renderChats() {
    // Проекты — фильтр списка чатов.
    const chips = [{ id: 'all', label: 'Все' }]
      .concat(state.projects.map((project) => ({ id: project.id, label: project.emoji + ' ' + project.name })))
      .concat([{ id: 'add', label: '＋ Проект' }]);
    dom.projectChips.replaceChildren(...chips.map((chip) => {
      const button = el('button', 'project-chip ripple' + (chip.id === 'add' ? ' project-chip--add' : ''), chip.label);
      button.type = 'button';
      button.dataset.project = String(chip.id);
      if (chip.id !== 'add') {
        button.setAttribute('role', 'tab');
        button.setAttribute('aria-selected', String(chip.id === state.chatFilter));
      }
      return button;
    }));
    const filtered = state.chatFilter !== 'all';
    dom.projectSettings.hidden = !filtered;
    const project = filtered ? projectById(state.chatFilter) : null;
    dom.chatsLabel.textContent = project ? 'Чаты проекта «' + project.name + '»' : 'Все чаты';

    const activeId = state.chat ? state.chat.id : null;
    const list = state.chats.filter((chat) => !filtered || chat.project_id === state.chatFilter);
    dom.chatList.replaceChildren(...list.map((chat) => {
      const li = el('li', 'chat-row' + (chat.id === activeId ? ' is-active' : '') + (state.chatMenu === chat.id ? ' is-open' : ''));
      const title = chat.title || (chat.id === activeId ? 'Новый чат' : 'Без названия');
      if (state.chatRename === chat.id) {
        const input = el('input', 'chat-row__input');
        input.type = 'text';
        input.maxLength = 60;
        input.value = chat.title;
        input.dataset.chatRenameInput = String(chat.id);
        input.setAttribute('aria-label', 'Название чата');
        input.enterKeyHint = 'done';
        li.append(input);
      } else {
        const main = el('button', 'chat-row__main ripple');
        main.type = 'button';
        main.dataset.chatOpen = String(chat.id);
        const owner = !filtered && chat.project_id != null ? projectById(chat.project_id) : null;
        main.append(
          el('span', 'chat-row__title', (owner ? owner.emoji + ' ' : '') + title),
          el('span', 'chat-row__meta', chatWhen(chat.updated))
        );
        const more = el('button', 'chat-row__more ripple');
        more.type = 'button';
        more.dataset.chatMore = String(chat.id);
        more.setAttribute('aria-label', 'Действия с чатом «' + title + '»');
        more.textContent = '⋯';
        li.append(main, more);
      }
      if (state.chatMenu === chat.id && state.chatRename !== chat.id) {
        const actions = el('div', 'chat-row__actions');
        if (state.chatDeleteAsk === chat.id) {
          actions.append(el('span', 'chat-row__ask', 'Удалить чат и его файлы?'));
          const yes = el('button', 'chat-action chat-action--danger ripple', 'Удалить');
          yes.type = 'button';
          yes.dataset.chatDeleteYes = String(chat.id);
          const no = el('button', 'chat-action ripple', 'Отмена');
          no.type = 'button';
          no.dataset.chatMore = String(chat.id);
          actions.append(no, yes);
        } else {
          const rename = el('button', 'chat-action ripple', '✏️ Переименовать');
          rename.type = 'button';
          rename.dataset.chatRename = String(chat.id);
          const remove = el('button', 'chat-action chat-action--danger ripple', '🗑 Удалить');
          remove.type = 'button';
          remove.dataset.chatDelete = String(chat.id);
          actions.append(rename, remove);
        }
        li.append(actions);
      }
      return li;
    }));
    dom.drawerEmpty.hidden = list.length > 0;
    const input = dom.chatList.querySelector('[data-chat-rename-input]');
    if (input) {
      input.focus();
      input.select();
    }
  }

  async function openChat(id) {
    if (state.chat && state.chat.id === id) {
      closeDrawer();
      return;
    }
    if (state.busy) {
      showToast('Дождись, пока ' + state.assistantName + ' ответит');
      return;
    }
    let data;
    try {
      data = await backend.chatOpen(id);
    } catch (error) {
      showToast(error.message);
      haptic('notify', 'error');
      return;
    }
    applyChats(data);
    closeDrawer(() => {
      clearNotice();
      renderHistory(data.history || []);
      haptic('impact', 'soft');
    });
  }

  async function renameChat(id, title) {
    state.chatRename = null;
    state.chatMenu = null;
    const chat = state.chats.find((item) => item.id === id);
    if (!title.trim() || (chat && chat.title === title.trim())) {
      renderChats();
      return;
    }
    try {
      applyChats(await backend.chatRename(id, title.trim()));
    } catch (error) {
      showToast(error.message);
    }
    renderChats();
  }

  async function deleteChat(id) {
    const wasOpen = state.chat && state.chat.id === id;
    let data;
    try {
      data = await backend.chatDelete(id);
    } catch (error) {
      showToast(error.message);
      haptic('notify', 'error');
      return;
    }
    state.chatMenu = null;
    state.chatDeleteAsk = null;
    applyChats(data);
    renderChats();
    if (wasOpen) renderHistory(data.history || []);
    showToast('Чат удалён');
    haptic('notify', 'success');
  }

  /* Нажатия в списке чатов и проектов (из общего обработчика шторки). */
  function chatsClick(event) {
    const chip = event.target.closest('[data-project]');
    if (chip) {
      const value = chip.dataset.project;
      if (value === 'add') {
        openProjectEditor(null);
        return true;
      }
      state.chatFilter = value === 'all' ? 'all' : Number(value);
      state.chatMenu = null;
      renderChats();
      haptic('select');
      return true;
    }
    const target = event.target.closest('[data-chat-open], [data-chat-more], [data-chat-rename], [data-chat-delete], [data-chat-delete-yes]');
    if (!target) return false;
    const data = target.dataset;
    if (data.chatOpen) openChat(Number(data.chatOpen));
    else if (data.chatMore) {
      const id = Number(data.chatMore);
      state.chatMenu = state.chatMenu === id && state.chatDeleteAsk !== id ? null : id;
      state.chatDeleteAsk = null;
      renderChats();
      haptic('select');
    } else if (data.chatRename) {
      state.chatRename = Number(data.chatRename);
      renderChats();
    } else if (data.chatDelete) {
      state.chatDeleteAsk = Number(data.chatDelete);
      renderChats();
      haptic('impact', 'light');
    } else if (data.chatDeleteYes) deleteChat(Number(data.chatDeleteYes));
    return true;
  }

  function bindChats() {
    dom.chatList.addEventListener('keydown', (event) => {
      const input = event.target.closest('[data-chat-rename-input]');
      if (!input) return;
      if (event.key === 'Enter') {
        event.preventDefault();
        input.blur();
      } else if (event.key === 'Escape') {
        input.value = '';
        input.blur();
      }
    });
    dom.chatList.addEventListener('focusout', (event) => {
      const input = event.target.closest('[data-chat-rename-input]');
      if (input) renameChat(Number(input.dataset.chatRenameInput), input.value);
    });
    dom.projectSettings.addEventListener('click', () => openProjectEditor(projectById(state.chatFilter)));
    dom.newChatBtn.addEventListener('click', () => startNewChat({ fromTopbar: true }));
  }

  /* ---------- Настройка проекта ---------- */

  function openProjectEditor(project) {
    closeDrawer(() => openSheet({ page: 'project', project }));
  }

  function fillProjectForm(project) {
    const editing = project ? Object.assign({}, project) : { id: null, name: '', emoji: '📚', instructions: '', mode: '' };
    state.projectEdit = editing;
    dom.projectName.value = editing.name;
    dom.projectInstructions.value = editing.instructions;
    renderProjectForm();
    dom.projectFilesBlock.hidden = editing.id === null;
    dom.projectDeleteBlock.hidden = editing.id === null;
    dom.projectDeleteConfirm.hidden = true;
    dom.projectDelete.hidden = false;
    dom.projectSave.textContent = editing.id === null ? 'Создать проект и чат в нём' : 'Сохранить';
    renderProjectFiles([]);
    if (editing.id !== null) loadProjectFiles(editing.id);
  }

  function renderProjectForm() {
    const editing = state.projectEdit;
    if (!editing) return;
    const emojis = PROJECT_EMOJIS.includes(editing.emoji) ? PROJECT_EMOJIS : [editing.emoji].concat(PROJECT_EMOJIS);
    dom.projectEmojis.replaceChildren(...emojis.map((emoji) => {
      const button = el('button', 'emoji-pick ripple', emoji);
      button.type = 'button';
      button.dataset.emoji = emoji;
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-checked', String(emoji === editing.emoji));
      return button;
    }));
    dom.projectCode.setAttribute('aria-checked', String(editing.mode === 'code'));
    dom.projectCodeBlock.hidden = state.mode === null;
    const length = dom.projectInstructions.value.length;
    dom.projectInstructionsCount.textContent = length + ' / 3000';
    dom.projectInstructionsCount.classList.toggle('is-near', length > 2700);
    dom.projectSave.disabled = !dom.projectName.value.trim();
  }

  function renderProjectFiles(files) {
    dom.projectFiles.replaceChildren(...files.map((file) => {
      const li = el('li', 'fact');
      const body = el('div', 'fact__body');
      body.append(el('span', 'fact__text', '📄 ' + file.name));
      const meta = el('div', 'fact__meta');
      meta.append(el('span', '', file.info));
      body.append(meta);
      const remove = el('button', 'fact__remove ripple');
      remove.type = 'button';
      remove.dataset.projectFile = String(file.id);
      remove.setAttribute('aria-label', 'Убрать файл «' + file.name + '»');
      remove.append(icon('i-close'));
      li.append(body, remove);
      return li;
    }));
    dom.projectFilesEmpty.hidden = files.length > 0;
  }

  async function loadProjectFiles(id) {
    try {
      const data = await backend.projectFiles(id);
      if (state.projectEdit && state.projectEdit.id === id) renderProjectFiles(data.files || []);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function addProjectFile(file) {
    const editing = state.projectEdit;
    if (!file || !editing || editing.id === null) return;
    if (!DOCUMENT_RE.test(file.name || '')) {
      showToast('Подойдут PDF, Word (.docx), Excel (.xlsx), CSV или текст.');
      return;
    }
    if (file.size > MAX_DOCUMENT_MB * 1024 * 1024) {
      showToast('Файл больше ' + MAX_DOCUMENT_MB + ' МБ.');
      return;
    }
    dom.projectFileAdd.disabled = true;
    dom.projectFileAdd.classList.add('is-loading');
    try {
      const data = await backend.projectFileUpload(editing.id, file);
      renderProjectFiles(data.files || []);
      if (data.documents !== undefined) setDocuments(data.documents);
      showToast(data.truncated ? 'Файл очень большой: прочитала только начало' : '📄 Файл добавлен в проект');
      haptic('notify', 'success');
    } catch (error) {
      showToast(error.message);
      haptic('notify', 'error');
    } finally {
      dom.projectFileAdd.disabled = false;
      dom.projectFileAdd.classList.remove('is-loading');
    }
  }

  async function removeProjectFile(id) {
    try {
      const data = await backend.documentsDelete(id);
      setDocuments(data.documents);
      if (state.projectEdit && state.projectEdit.id !== null) loadProjectFiles(state.projectEdit.id);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function saveProject() {
    const editing = state.projectEdit;
    if (!editing || !dom.projectName.value.trim()) return;
    const creating = editing.id === null;
    dom.projectSave.disabled = true;
    let data;
    try {
      data = await backend.projectSave({
        id: editing.id || undefined,
        name: dom.projectName.value,
        emoji: editing.emoji,
        instructions: dom.projectInstructions.value,
        mode: editing.mode,
        open: creating,
      });
    } catch (error) {
      dom.projectSave.disabled = false;
      showToast(error.message);
      haptic('notify', 'error');
      return;
    }
    applyChats(data);
    closeSheet();
    if (creating) {
      state.chatFilter = data.project.id;
      renderHistory(data.history || []);
      showToast(data.project.emoji + ' Проект «' + data.project.name + '» создан — это его первый чат');
    } else {
      showToast('Проект сохранён');
    }
    haptic('notify', 'success');
  }

  async function deleteProject() {
    const editing = state.projectEdit;
    if (!editing || editing.id === null) return;
    let data;
    try {
      data = await backend.projectDelete(editing.id);
    } catch (error) {
      showToast(error.message);
      return;
    }
    state.chatFilter = 'all';
    applyChats(data);
    closeSheet();
    showToast('Проект удалён, его чаты — в «Без проекта»');
    haptic('notify', 'success');
  }

  function bindProjectEditor() {
    dom.projectEmojis.addEventListener('click', (event) => {
      const button = event.target.closest('[data-emoji]');
      if (!button || !state.projectEdit) return;
      state.projectEdit.emoji = button.dataset.emoji;
      renderProjectForm();
      haptic('select');
    });
    dom.projectName.addEventListener('input', renderProjectForm);
    dom.projectName.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        dom.projectInstructions.focus();
      }
    });
    dom.projectInstructions.addEventListener('input', renderProjectForm);
    dom.projectCode.addEventListener('click', () => {
      if (!state.projectEdit) return;
      state.projectEdit.mode = state.projectEdit.mode === 'code' ? '' : 'code';
      renderProjectForm();
      haptic('select');
    });
    dom.projectFileAdd.addEventListener('click', () => dom.projectFileInput.click());
    dom.projectFileInput.addEventListener('change', () => {
      const file = dom.projectFileInput.files && dom.projectFileInput.files[0];
      dom.projectFileInput.value = '';
      addProjectFile(file);
    });
    dom.projectFiles.addEventListener('click', (event) => {
      const button = event.target.closest('[data-project-file]');
      if (button) removeProjectFile(Number(button.dataset.projectFile));
    });
    dom.projectSave.addEventListener('click', saveProject);
    dom.projectDelete.addEventListener('click', () => {
      dom.projectDelete.hidden = true;
      dom.projectDeleteConfirm.hidden = false;
      haptic('impact', 'light');
    });
    dom.projectDeleteCancel.addEventListener('click', () => {
      dom.projectDelete.hidden = false;
      dom.projectDeleteConfirm.hidden = true;
    });
    dom.projectDeleteYes.addEventListener('click', deleteProject);
  }

  function renderDrawer() {
    dom.drawerTitle.textContent = state.assistantName;
    dom.drawerInitials.textContent = initials(state.firstName);
    dom.drawerDraw.hidden = !(state.images && state.images.enabled);
    dom.drawerVideo.hidden = !state.video.available;
    const clips = state.video.session ? state.video.session.clips.length : 0;
    dom.drawerVideoMeta.textContent = state.video.session
      ? (clips ? clips + ' ' + plural(clips, ['клип', 'клипа', 'клипов']) : 'начат')
      : '';
    dom.drawerCode.hidden = state.mode === null;
    dom.drawerCodeMeta.textContent = state.mode === 'code' ? 'вкл.' : 'выкл.';
    const pending = (state.reminders.items || []).length;
    dom.drawerReminders.textContent = pending ? String(pending) : '';
    dom.drawerReminders.classList.toggle('is-badge', pending > 0);
    const facts = (state.memory.facts || []).length;
    dom.drawerMemory.textContent = state.memory.enabled
      ? (facts ? facts + ' ' + plural(facts, ['факт', 'факта', 'фактов']) : 'вкл.')
      : 'выкл.';
    dom.drawerInstructions.hidden = !state.instructions;
    const instr = state.instructions;
    dom.drawerInstructionsMeta.textContent = !instr || (!instr.about && !instr.style)
      ? 'не заданы'
      : instr.enabled ? 'вкл.' : 'выкл.';
    state.chatMenu = null;
    state.chatRename = null;
    state.chatDeleteAsk = null;
    renderChats();
  }

  function openDrawer() {
    if (!dom.drawer.hidden && !state.drawerClosing) return;
    if (state.finishReveal) state.finishReveal();
    renderDrawer();
    if (state.drawerAnim) state.drawerAnim.cancel();
    state.drawerClosing = false;
    dom.drawer.getAnimations().forEach((animation) => animation.cancel());
    dom.drawerBackdrop.getAnimations().forEach((animation) => animation.cancel());
    dom.drawer.style.transform = '';
    dom.drawerBackdrop.style.opacity = '';
    dom.drawerBackdrop.hidden = false;
    dom.drawer.hidden = false;
    dom.drawerScroll.scrollTop = 0;
    dom.input.blur();
    state.drawerAnim = motion(dom.drawer, [{ transform: 'perspective(1200px) translateX(-102%) rotateY(32deg)' }, { transform: 'none' }],
      { duration: 460, easing: 'cubic-bezier(0.2, 1.04, 0.32, 1)' });
    motion(dom.drawerBackdrop, [{ opacity: 0 }, { opacity: 1 }], { duration: 280, easing: 'ease-out' });
    dom.drawerBtn.setAttribute('aria-expanded', 'true');
    if (supports('6.1')) tg.BackButton.show();
    haptic('impact', 'light');
    if (finePointer) dom.drawerClose.focus();
  }

  /* then — что сделать, когда шторка закроется (например, открыть настройки). */
  function closeDrawer(then) {
    if (dom.drawer.hidden || state.drawerClosing) {
      if (typeof then === 'function') then();
      return;
    }
    state.drawerClosing = true;
    if (supports('6.1')) tg.BackButton.hide();
    dom.drawerBtn.setAttribute('aria-expanded', 'false');
    const fromTransform = dom.drawer.style.transform || 'none';
    const fromOpacity = dom.drawerBackdrop.style.opacity || '1';
    if (state.drawerAnim) state.drawerAnim.cancel();
    const out = motion(dom.drawer, [{ transform: fromTransform }, { transform: 'perspective(1200px) translateX(-102%) rotateY(32deg)' }],
      { duration: 240, easing: 'cubic-bezier(0.4, 0, 0.9, 0.6)', fill: 'forwards' });
    const fade = motion(dom.drawerBackdrop, [{ opacity: fromOpacity }, { opacity: 0 }], { duration: 240, fill: 'forwards' });
    const done = () => {
      dom.drawer.hidden = true;
      dom.drawerBackdrop.hidden = true;
      if (out) out.cancel();
      if (fade) fade.cancel();
      dom.drawer.style.transform = '';
      dom.drawerBackdrop.style.opacity = '';
      dom.drawer.classList.remove('is-dragging');
      state.drawerClosing = false;
      if (typeof then === 'function') then();
    };
    if (out) out.onfinish = done;
    else done();
  }

  function drawerAction(action) {
    if (action === 'draw') {
      closeDrawer(() => {
        if (!/^нарисуй/i.test(dom.input.value.trim())) dom.input.value = 'Нарисуй ' + dom.input.value.trim();
        autosize();
        updateComposer();
        dom.input.focus();
        const end = dom.input.value.length;
        dom.input.setSelectionRange(end, end);
      });
    } else if (action === 'video') {
      closeDrawer(() => startVideo());
    } else if (action === 'code') {
      closeDrawer(() => toggleMode());
    } else if (action === 'reminders' || action === 'memory') {
      closeDrawer(() => openSheet({ focus: action }));
    } else if (action === 'instructions') {
      closeDrawer(() => {
        openSheet();
        setTimeout(openInstructionsPage, reducedMotion ? 0 : 320);
      });
    } else if (action === 'settings') {
      closeDrawer(() => openSheet());
    }
  }

  /* «Новый чат»: открывается пустой чат, прошлый остаётся в списке чатов.
     Из шторки — в выбранном там проекте («Все» — без проекта), из шапки — в проекте открытого. */
  async function startNewChat(options) {
    const fromTopbar = Boolean(options && options.fromTopbar);
    if (dom.drawerNew.disabled) return;
    if (state.busy) {
      showToast('Дождись, пока ' + state.assistantName + ' ответит');
      return;
    }
    const projectId = fromTopbar ? undefined : (state.chatFilter === 'all' ? null : state.chatFilter);
    dom.drawerNew.disabled = true;
    let data;
    try {
      data = await backend.newChat(projectId);
    } catch (error) {
      showToast(error.message);
      haptic('notify', 'error');
      return;
    } finally {
      dom.drawerNew.disabled = false;
    }
    applyChats(data);
    closeDrawer(async () => {
      clearNotice();
      if (dom.messages.children.length && !reducedMotion) await wipeFeedQuietly();
      renderHistory(data.history || []);
      const project = currentProject();
      showToast(project ? 'Новый чат в «' + project.emoji + ' ' + project.name + '»' : 'Новый чат');
      haptic('notify', 'success');
      if (finePointer) dom.input.focus();
    });
  }

  /* Сообщения прошлого чата разлетаются, перед тем как открыть новый. */
  async function wipeFeedQuietly() {
    const nodes = Array.from(dom.messages.children).slice(-10).reverse();
    nodes.forEach((node, index) => {
      node.style.setProperty('--i', index);
      node.classList.add('is-leaving');
    });
    await sleep(280 + nodes.length * 25);
  }

  /* Шторку можно смахнуть влево. Вертикальная прокрутка внутри работает как обычно. */
  function bindDrawerDrag() {
    let pointer = null;
    let startX = 0;
    let startY = 0;
    let lastX = 0;
    let lastTime = 0;
    let velocity = 0;
    let width = 0;
    let dragging = false;

    dom.drawer.addEventListener('pointerdown', (event) => {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      pointer = event.pointerId;
      startX = lastX = event.clientX;
      startY = event.clientY;
      lastTime = event.timeStamp;
      velocity = 0;
      dragging = false;
      state.drawerDragged = false;
      width = dom.drawer.offsetWidth;
    });

    dom.drawer.addEventListener('pointermove', (event) => {
      if (event.pointerId !== pointer) return;
      const dx = event.clientX - startX;
      const dy = event.clientY - startY;
      if (!dragging) {
        if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) {
          pointer = null; // это прокрутка списка
          return;
        }
        if (dx > -10 || Math.abs(dx) < Math.abs(dy)) return;
        dragging = true;
        state.drawerDragged = true;
        if (state.drawerAnim) state.drawerAnim.cancel();
        dom.drawer.classList.add('is-dragging');
        try {
          dom.drawer.setPointerCapture(event.pointerId);
        } catch (error) { /* указатель уже ушёл */ }
      }
      const offset = Math.min(0, dx);
      dom.drawer.style.transform = 'translateX(' + offset + 'px)';
      dom.drawerBackdrop.style.opacity = String(Math.max(0, 1 + offset / width));
      const dt = event.timeStamp - lastTime;
      if (dt > 0) velocity = (event.clientX - lastX) / dt;
      lastX = event.clientX;
      lastTime = event.timeStamp;
    });

    const up = (event) => {
      if (event.pointerId !== pointer) return;
      pointer = null;
      if (!dragging) return;
      dragging = false;
      dom.drawer.classList.remove('is-dragging');
      const dx = event.clientX - startX;
      if (dx < -Math.min(120, width * 0.3) || (dx < -10 && velocity < -0.5)) {
        haptic('impact', 'light');
        closeDrawer();
        return;
      }
      // Не дотянули — шторка пружинит обратно.
      const fromTransform = dom.drawer.style.transform || 'none';
      const fromOpacity = dom.drawerBackdrop.style.opacity || '1';
      dom.drawer.style.transform = '';
      dom.drawerBackdrop.style.opacity = '';
      motion(dom.drawer, [{ transform: fromTransform }, { transform: 'none' }], { duration: 420, easing: SPRING });
      motion(dom.drawerBackdrop, [{ opacity: fromOpacity }, { opacity: 1 }], { duration: 260, easing: 'ease-out' });
    };
    dom.drawer.addEventListener('pointerup', up);
    dom.drawer.addEventListener('pointercancel', up);
  }

  function bindDrawer() {
    bindDrawerDrag();
    dom.drawerBtn.addEventListener('click', openDrawer);
    dom.drawerClose.addEventListener('click', () => closeDrawer());
    dom.drawerBackdrop.addEventListener('click', () => closeDrawer());
    dom.drawer.addEventListener('click', (event) => {
      if (state.drawerDragged) {
        // Это был свайп, а не нажатие.
        state.drawerDragged = false;
        return;
      }
      const item = event.target.closest('[data-drawer]');
      if (item) {
        drawerAction(item.dataset.drawer);
        return;
      }
      chatsClick(event);
    });
    dom.drawerNew.addEventListener('click', () => startNewChat());
    bindChats();
    bindProjectEditor();
    bindGitHub();
    dom.drawerMe.addEventListener('click', () => closeDrawer(() => openSheet()));
  }

  /* ---------- Загрузка и синхронизация ---------- */

  function applyState(data) {
    state.assistantName = (data.assistant && data.assistant.name) || 'Эля';
    state.firstName = (data.user && data.user.first_name) || '';
    state.models = data.models || [];
    state.providers = data.providers || [];
    state.codeLines = (data.about && data.about.code_lines) || 0;
    state.usage = data.usage || state.usage;
    state.maxLength = (data.limits && data.limits.max_text_length) || 4000;
    state.voice = Boolean(data.voice);
    state.vision = Boolean(data.vision);
    state.images = data.images || { enabled: false };
    state.search = Boolean(data.search);
    if (!state.search && state.searchNext) setSearchNext(false);
    if (data.preferences) state.preferences = data.preferences;
    if (data.memory) state.memory = Object.assign({}, state.memory, data.memory);
    if (data.reminders) state.reminders = Object.assign({}, state.reminders, data.reminders);
    state.instructions = data.instructions || null;
    state.mode = data.mode === undefined ? null : data.mode;
    applyVideo(data.video);
    setDocuments(data.documents === undefined ? null : data.documents);
    state.github = data.github === undefined ? null : data.github;
    state.chat = data.chat || null;
    state.chats = data.chats || [];
    state.projects = data.projects || [];
    renderInstructionsSummary();
    if (data.preference_options) state.preferenceOptions = data.preference_options;
    state.syncedAt = Date.now();

    const saved = store(MODEL_KEY);
    state.model = state.models.some((item) => item.id === saved) ? saved : AUTO;

    const name = state.assistantName;
    document.title = name;
    dom.assistantName.textContent = name;
    dom.welcomeName.textContent = name;
    renderMode();
    renderChatHeader();
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
    openPreviewFromLink();
    if (query.get('open') === 'instructions' && state.instructions) {
      setTimeout(() => {
        openSheet();
        setTimeout(openInstructionsPage, reducedMotion ? 0 : 320);
      }, reducedMotion ? 0 : 500);
    } else if (finePointer) dom.input.focus();
  }

  /* Сообщения, отправленные тем временем в чате с ботом, подтягиваются при возврате. */
  async function sync() {
    if (dom.app.hidden || state.busy || state.finishReveal || !dom.drawer.hidden || Date.now() - state.syncedAt < 5000) return;
    state.syncedAt = Date.now();
    let data;
    try {
      data = await backend.state();
    } catch (error) {
      return;
    }
    if (state.busy || state.finishReveal) return;
    state.usage = data.usage || state.usage;
    // Режим могли переключить в чате с ботом командой /code.
    if (data.mode !== undefined && data.mode !== state.mode && dom.sheet.hidden) takeMode(data);
    if (data.memory && dom.sheet.hidden) state.memory = Object.assign({}, state.memory, data.memory);
    // Монтаж могли начать или закончить в чате с ботом.
    if (data.video) applyVideo(data.video);
    // Документы могли прислать или убрать в чате с ботом, а чат — сменить командой /chats.
    if (data.documents !== undefined) setDocuments(data.documents);
    if (data.chat) applyChats({ chat: data.chat, chats: data.chats, projects: data.projects });
    if (data.reminders && dom.sheet.hidden) {
      state.reminders = Object.assign({}, state.reminders, data.reminders);
      updateBell();
    }
    setStatus('online');
    const history = data.history || [];
    const signature = historySignature(history);
    if (signature !== state.signature && !state.notice) {
      if (state.signature === '' && history.length === dom.messages.querySelectorAll('.msg, .chat-break').length) {
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
    dom.searchToggle.addEventListener('mousedown', (event) => event.preventDefault());
    dom.searchToggle.addEventListener('click', () => {
      setSearchNext(!state.searchNext);
      haptic('selection');
      if (state.searchNext) showToast('🌐 Поищу ответ в интернете и приложу ссылки на источники');
    });
    dom.fileInput.addEventListener('change', () => {
      const file = dom.fileInput.files && dom.fileInput.files[0];
      dom.fileInput.value = ''; // чтобы тот же файл можно было выбрать ещё раз
      pickFile(file);
    });
    dom.videoDone.addEventListener('click', finishVideo);
    dom.attachmentRemove.addEventListener('click', () => clearAttachment(false));
    dom.recCancel.addEventListener('click', () => stopRecording(true));

    dom.suggestions.addEventListener('click', (event) => {
      const card = event.target.closest('.suggestion');
      if (card) send(card.dataset.prompt, null, { kind: 'card', rect: card.getBoundingClientRect() });
    });

    dom.messages.addEventListener('click', (event) => {
      const videoAction = event.target.closest('[data-video-action]');
      if (videoAction) {
        runVideoAction(videoAction.dataset.videoAction, videoAction.dataset.value || '', videoAction.textContent);
        return;
      }
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

    bindDrawer();
    dom.menuBtn.addEventListener('click', () => openSheet());
    dom.aboutBtn.addEventListener('click', () => openSheet({ page: 'about' }));
    dom.aboutAuthor.addEventListener('click', (event) => {
      if (!supports('6.1')) return; // вне Telegram ссылка откроется сама
      event.preventDefault();
      tg.openTelegramLink(dom.aboutAuthor.href);
    });
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
        if (closePreview()) return;
        if (!dom.drawer.hidden) closeDrawer();
        else if (state.sheetPage === 'instructions') leaveInstructionsPage();
        else closeSheet();
      }
    });

    dom.codeToggle.addEventListener('click', toggleMode);
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
      if (supports('6.1')) {
        tg.BackButton.onClick(() => {
          if (closePreview()) return;
          if (!dom.drawer.hidden) closeDrawer();
          else if (state.sheetPage === 'instructions') leaveInstructionsPage();
          else closeSheet();
        });
      }
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
