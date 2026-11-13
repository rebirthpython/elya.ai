/* Markdown → DOM для ответов модели.
 *
 * Узлы создаются через createElement/textContent, без innerHTML, поэтому текст
 * модели не может превратиться в разметку или скрипт. Поддерживаются: абзацы,
 * заголовки, списки (в том числе вложенные), цитаты, таблицы, блоки кода,
 * `код`, **жирный**, *курсив*, ~~зачёркнутый~~ и ссылки http(s).
 */
(function () {
  'use strict';

  const WORD_CHAR = /[\p{L}\p{N}_]/u;

  const INLINE = new RegExp(
    [
      '`([^`\\n]+)`',                                        // 1: код
      '\\*\\*(\\S(?:[\\s\\S]*?\\S)?)\\*\\*',                 // 2: **жирный**
      '__(\\S(?:[\\s\\S]*?\\S)?)__',                         // 3: __жирный__
      '~~(\\S(?:[\\s\\S]*?\\S)?)~~',                         // 4: ~~зачёркнутый~~
      '\\*([^\\s*](?:[^*\\n]*?[^\\s*])?)\\*',                // 5: *курсив*
      '_([^\\s_](?:[^_\\n]*?[^\\s_])?)_',                    // 6: _курсив_
      '\\[([^\\]\\n]+)\\]\\((https?:\\/\\/(?:[^\\s()]|\\([^\\s()]*\\))+)\\)', // 7, 8: [текст](ссылка)
      '(https?:\\/\\/[^\\s<>"\'`]+)',                        // 9: голая ссылка
    ].join('|'),
    'g'
  );

  const FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
  const HEADING = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
  const RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
  const QUOTE = /^\s{0,3}>\s?(.*)$/;
  const ITEM = /^(\s*)(?:([-*+•])|(\d{1,9})[.)])\s+(.*)$/;
  const TABLE_ROW = /^\s*\|.*\|\s*$/;
  const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function isWordChar(char) {
    return char !== undefined && WORD_CHAR.test(char);
  }

  function link(url) {
    const a = el('a');
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    return a;
  }

  function trimUrl(url) {
    // Знаки препинания в конце фразы не относятся к ссылке.
    let end = url.length;
    while (end > 0) {
      const char = url[end - 1];
      const head = url.slice(0, end);
      const unbalanced = char === ')' && head.split(')').length > head.split('(').length;
      if (!unbalanced && !/[.,;:!?…»"'\]]/.test(char)) break;
      end -= 1;
    }
    return url.slice(0, end);
  }

  function appendInline(parent, text, options) {
    const allowLinks = !options || options.links !== false;
    const re = new RegExp(INLINE.source, 'g');
    let last = 0;
    let match;

    while ((match = re.exec(text)) !== null) {
      const start = match.index;
      let end = re.lastIndex;
      let node = null;

      if (match[1] !== undefined) {
        node = el('code');
        node.textContent = match[1];
      } else if (match[2] !== undefined || match[3] !== undefined) {
        const inner = match[2] !== undefined ? match[2] : match[3];
        const glued = match[3] !== undefined && (isWordChar(text[start - 1]) || isWordChar(text[end]));
        if (!glued) {
          node = el('strong');
          appendInline(node, inner, options);
        }
      } else if (match[4] !== undefined) {
        node = el('s');
        appendInline(node, match[4], options);
      } else if (match[5] !== undefined || match[6] !== undefined) {
        // «2*3*4» и snake_case — не курсив: маркер не должен примыкать к слову.
        const before = text[start - 1];
        const after = text[end];
        const marker = match[5] !== undefined ? '*' : '_';
        const glued = isWordChar(before) || isWordChar(after) || before === marker || after === marker;
        if (!glued) {
          node = el('em');
          appendInline(node, match[5] !== undefined ? match[5] : match[6], options);
        }
      } else if (match[7] !== undefined) {
        if (allowLinks) {
          node = link(match[8]);
          appendInline(node, match[7], { links: false });
        }
      } else if (match[9] !== undefined) {
        if (allowLinks) {
          const url = trimUrl(match[9]);
          if (url.length > 'https://'.length) {
            node = link(url);
            node.textContent = url;
            end = start + url.length;
          }
        }
      }

      if (node === null) {
        // Не разметка: пропускаем один символ и ищем дальше.
        re.lastIndex = start + 1;
        continue;
      }
      if (start > last) parent.append(text.slice(last, start));
      parent.append(node);
      last = end;
      re.lastIndex = end;
    }
    if (last < text.length) parent.append(text.slice(last));
  }

  function appendLines(parent, lines) {
    lines.forEach((line, index) => {
      if (index > 0) parent.append(el('br'));
      appendInline(parent, line);
    });
  }

  function codeBlock(code, language) {
    const box = el('div', 'code');
    const head = el('div', 'code__head');
    const label = el('span');
    label.textContent = language || 'код';
    const copy = el('button', 'chip-btn');
    copy.type = 'button';
    copy.dataset.copy = code;
    copy.textContent = 'Копировать';
    head.append(label, copy);

    const pre = el('pre');
    const inner = el('code');
    inner.textContent = code;
    pre.append(inner);
    box.append(head, pre);
    return box;
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  }

  function table(headerLine, bodyLines) {
    const wrap = el('div', 'table-wrap');
    const node = el('table');
    const head = el('thead');
    const headRow = el('tr');
    const header = splitRow(headerLine);
    header.forEach((cell) => {
      const th = el('th');
      appendInline(th, cell);
      headRow.append(th);
    });
    head.append(headRow);

    const body = el('tbody');
    bodyLines.forEach((line) => {
      const row = el('tr');
      const cells = splitRow(line);
      for (let i = 0; i < header.length; i += 1) {
        const td = el('td');
        appendInline(td, cells[i] || '');
        row.append(td);
      }
      body.append(row);
    });
    node.append(head, body);
    wrap.append(node);
    return wrap;
  }

  /* Список: вложенность определяется отступом пункта. */
  function list(items) {
    const root = { indent: -1, node: null, lastItem: null };
    const stack = [root];
    let container = null;

    items.forEach((item) => {
      while (stack.length > 1 && item.indent < stack[stack.length - 1].indent) stack.pop();
      let level = stack[stack.length - 1];

      if (level === root || item.indent > level.indent) {
        const node = el(item.ordered ? 'ol' : 'ul');
        if (item.ordered && item.start !== 1) node.start = item.start;
        if (level === root) container = node;
        else (level.lastItem || level.node).append(node);
        level = { indent: item.indent, node, lastItem: null };
        stack.push(level);
      }

      const li = el('li');
      appendLines(li, item.lines);
      level.node.append(li);
      level.lastItem = li;
    });
    return container;
  }

  function render(source) {
    const root = el('div', 'md');
    const lines = String(source).replace(/\r\n?/g, '\n').split('\n');
    let i = 0;

    const isBlockStart = (line) =>
      FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || ITEM.test(line);

    while (i < lines.length) {
      const line = lines[i];

      if (!line.trim()) {
        i += 1;
        continue;
      }

      const fence = line.match(FENCE);
      if (fence) {
        const closing = new RegExp('^\\s{0,3}' + fence[1][0] + '{' + fence[1].length + ',}\\s*$');
        const code = [];
        i += 1;
        while (i < lines.length && !closing.test(lines[i])) {
          code.push(lines[i]);
          i += 1;
        }
        i += 1; // закрывающая строка (или конец текста)
        root.append(codeBlock(code.join('\n'), fence[2]));
        continue;
      }

      const heading = line.match(HEADING);
      if (heading) {
        const node = el('h' + Math.min(5, heading[1].length + 2));
        appendInline(node, heading[2]);
        root.append(node);
        i += 1;
        continue;
      }

      if (RULE.test(line)) {
        root.append(el('hr'));
        i += 1;
        continue;
      }

      if (QUOTE.test(line)) {
        const quoted = [];
        while (i < lines.length && QUOTE.test(lines[i])) {
          quoted.push(lines[i].match(QUOTE)[1]);
          i += 1;
        }
        const node = el('blockquote');
        appendLines(node, quoted);
        root.append(node);
        continue;
      }

      if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_DIVIDER.test(lines[i + 1]) && lines[i + 1].includes('-')) {
        const body = [];
        let j = i + 2;
        while (j < lines.length && TABLE_ROW.test(lines[j])) {
          body.push(lines[j]);
          j += 1;
        }
        root.append(table(line, body));
        i = j;
        continue;
      }

      if (ITEM.test(line)) {
        const items = [];
        while (i < lines.length) {
          const current = lines[i];
          const item = current.match(ITEM);
          if (item && !RULE.test(current)) {
            const indent = item[1].replace(/\t/g, '    ').length;
            const ordered = item[3] !== undefined;
            // Пункт левее первого или смена типа на верхнем уровне — это уже новый список.
            if (items.length && (indent < items[0].indent || (indent === items[0].indent && ordered !== items[0].ordered))) break;
            items.push({
              indent,
              ordered,
              start: ordered ? Number(item[3]) : 1,
              lines: [item[4]],
            });
            i += 1;
          } else if (current.trim() && /^\s+/.test(current) && !FENCE.test(current)) {
            items[items.length - 1].lines.push(current.trim()); // продолжение пункта
            i += 1;
          } else if (!current.trim() && i + 1 < lines.length && ITEM.test(lines[i + 1])) {
            i += 1; // пустая строка между пунктами
          } else {
            break;
          }
        }
        root.append(list(items));
        continue;
      }

      const paragraph = [];
      while (i < lines.length && lines[i].trim() && !(paragraph.length && isBlockStart(lines[i]))) {
        paragraph.push(lines[i].trim());
        i += 1;
      }
      const node = el('p');
      appendLines(node, paragraph);
      root.append(node);
    }
    return root;
  }

  window.ElyaMarkdown = { render };
})();
