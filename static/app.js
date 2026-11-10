(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const ui = { refresh: $('refresh'), scanTime: $('scan-time'), sources: $('sources-list'), sourceNav: $('sources-nav'), projectNav: $('projects-nav'), search: $('search'), source: $('source-filter'), project: $('project-filter'), messageFilter: $('message-filter'), messageFilterLabel: $('message-filter-label'), count: $('session-count'), listState: $('list-state'), list: $('session-list'), more: $('show-more'), detail: $('detail'), announce: $('announcement') };
  const pageSize = 100;
  let sessions = [], sources = [], visibleLimit = pageSize, scanRequest = 0, contentRequest = 0, searchTimer;
  let state = readUrl();

  function readUrl() {
    const p = new URLSearchParams(location.search);
    return { q: p.get('q') || '', source: p.get('source') || '', project: p.get('project') || '', id: p.get('session') || '', view: p.get('view') === 'files' ? 'files' : 'session', fileSource: p.get('fs') || '', path: p.get('path') || '', file: p.get('file') === '1', hasMessages: p.get('has') === '1' };
  }
  function writeUrl(mode = 'push') {
    const url = new URL(location.href);
    const values = { q: state.q, source: state.source, project: state.project, session: state.id, view: state.view === 'files' ? 'files' : '', fs: state.view === 'files' ? state.fileSource : '', path: state.view === 'files' ? state.path : '', file: state.view === 'files' && state.file ? '1' : '', has: state.hasMessages ? '1' : '' };
    for (const [key, value] of Object.entries(values)) value ? url.searchParams.set(key, value) : url.searchParams.delete(key);
    if (url.href !== location.href) history[mode === 'replace' ? 'replaceState' : 'pushState'](null, '', url);
  }
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }
  function button(text, className, action) {
    const el = node('button', className, text); el.type = 'button'; el.addEventListener('click', action); return el;
  }
  function shown(value, fallback = 'Не указано') { return value === undefined || value === null || value === '' ? fallback : String(value); }
  function dateValue(value) {
    if (value === undefined || value === null || value === '') return 0;
    const numeric = typeof value === 'number' || (typeof value === 'string' && /^\d+(\.\d+)?$/.test(value));
    const time = numeric ? Number(value) * 1000 : Date.parse(value);
    return Number.isFinite(time) ? time : 0;
  }
  function dateText(value) { const t = dateValue(value); return t ? new Date(t).toLocaleString('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }) : shown(value); }
  function announce(text) { ui.announce.textContent = text; }
  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { 'X-Harness-Token': window.HARNESS_TOKEN || '', ...(options.headers || {}) }, cache: 'no-store' });
    let body;
    try { body = await response.json(); } catch (_) { throw new Error(`Сервер вернул нечитаемый ответ (${response.status}).`); }
    if (!response.ok) throw new Error(typeof body.error === 'string' ? body.error : `Ошибка запроса (${response.status}).`);
    return body;
  }
  function message(title, description, error = false) {
    const box = node('div', `empty-detail${error ? ' error' : ''}`);
    const h = node('h2', '', title); h.id = 'detail-heading'; box.append(h, node('p', '', description)); return box;
  }
  function selectSession(id) {
    if (!id) return;
    clearTimeout(searchTimer); state.id = id; state.view = 'session'; writeUrl(); renderList(); loadContent();
  }
  function openFiles(source, path = '', file = false) {
    if (!source) return;
    clearTimeout(searchTimer); state.view = 'files'; state.fileSource = source; state.path = path; state.file = file; writeUrl(); renderNavigation(); loadContent();
  }
  function updateOptions(select, values, selected, label) {
    const unique = [...new Set(values.filter(Boolean).map(String))].sort((a, b) => a.localeCompare(b, 'ru'));
    if (selected && !unique.includes(selected)) unique.push(selected);
    select.replaceChildren(); const all = node('option', '', label); all.value = ''; select.append(all);
    for (const value of unique) { const opt = node('option', '', value); opt.value = value; select.append(opt); }
    select.value = selected;
  }
  function projectKey(session) {
    return session.project ? JSON.stringify([session.remote ? 'vps' : 'local', session.project]) : '';
  }
  function projectEntries() {
    const byKey = new Map();
    for (const session of sessions) {
      const key = projectKey(session);
      if (key && !byKey.has(key)) byKey.set(key, { key, path: String(session.project), remote: !!session.remote });
    }
    return [...byKey.values()].sort((a, b) => a.path.localeCompare(b.path, 'ru') || Number(a.remote) - Number(b.remote));
  }
  function updateControls() {
    ui.search.value = state.q;
    updateOptions(ui.source, [...sources.map(s => s.name), ...sessions.map(s => s.source)], state.source, 'Все источники');
    const projects = projectEntries();
    ui.project.replaceChildren();
    const all = node('option', '', 'Все проекты'); all.value = ''; ui.project.append(all);
    for (const project of projects) {
      const option = node('option', '', project.path + (project.remote ? ' (VPS)' : ''));
      option.value = project.key; ui.project.append(option);
    }
    if (state.project && !projects.some(project => project.key === state.project)) {
      const missing = node('option', '', 'Проект недоступен'); missing.value = state.project; ui.project.append(missing);
    }
    ui.project.value = state.project;
    ui.messageFilterLabel.hidden = state.source !== 'Hermes VPS';
    ui.messageFilter.checked = state.hasMessages;
  }
  function setFilter(key, value) {
    clearTimeout(searchTimer); state[key] = value; visibleLimit = pageSize; updateControls(); writeUrl(); renderNavigation(); renderList();
  }
  function renderNavigation() {
    ui.projectNav.replaceChildren(); ui.sourceNav.replaceChildren(); ui.sources.replaceChildren();
    const allProjects = button('Все проекты', 'nav-button', () => setFilter('project', ''));
    if (!state.project) allProjects.setAttribute('aria-current', 'true'); ui.projectNav.append(allProjects);
    const projects = projectEntries();
    const names = projects.map(project => project.path.split('/').filter(Boolean).pop() || project.path);
    for (const [index, project] of projects.entries()) {
      const parts = project.path.split('/').filter(Boolean);
      const base = names.filter(name => name === names[index]).length > 1 && parts.length > 1
        ? `${names[index]} / ${parts[parts.length - 2]}` : names[index];
      const maxLength = project.remote ? 15 : 21;
      const shortName = base.length > maxLength ? `${base.slice(0, maxLength - 1)}…` : base;
      const label = shortName + (project.remote ? ' (VPS)' : '');
      const b = button(label, 'nav-button', () => setFilter('project', project.key)); b.title = project.path + (project.remote ? ' (VPS)' : '');
      if (state.project === project.key) b.setAttribute('aria-current', 'true'); ui.projectNav.append(b);
    }
    const allSources = button('Все источники', 'nav-button', () => setFilter('source', ''));
    if (!state.source) allSources.setAttribute('aria-current', 'true'); ui.sourceNav.append(allSources);
    const sourceNames = [...new Set([...sources.map(s => s.name), ...sessions.map(s => s.source)].filter(Boolean))];
    for (const name of sourceNames) {
      const row = node('div', 'source-nav-row');
      const filter = button(name, 'nav-button', () => setFilter('source', name));
      if (state.source === name) filter.setAttribute('aria-current', 'true');
      const browse = button('Файлы', 'nav-files', () => openFiles(name)); browse.setAttribute('aria-label', `Файлы источника ${name}`);
      row.append(filter, browse); ui.sourceNav.append(row);
    }
    if (!sources.length) ui.sources.append(node('p', 'subtle', 'Сведения об источниках не получены.'));
    const statuses = { ok: 'Доступен', empty: 'Пусто', missing: 'Не найден', error: 'Ошибка' };
    for (const source of sources) {
      const item = node('div', 'source-item');
      item.append(node('strong', '', shown(source.name, 'Неизвестный источник')), node('span', 'source-status', statuses[source.status] || shown(source.status, 'Неизвестно')));
      if (source.root) item.append(node('div', 'source-root', source.root));
      if (source.detail) item.append(node('p', 'source-detail', source.detail));
      ui.sources.append(item);
    }
  }
  function matches(session) {
    if (state.source && session.source !== state.source || state.project && projectKey(session) !== state.project) return false;
    if (state.source === 'Hermes VPS' && state.hasMessages && !(session.message_count > 0)) return false;
    const query = state.q.trim().toLocaleLowerCase();
    return !query || [session.title, session.project, session.path, session.id, session.model, session.source, session.status].some(v => String(v || '').toLocaleLowerCase().includes(query));
  }
  function renderList() {
    ui.list.replaceChildren();
    const filtered = sessions.filter(matches).sort((a, b) => dateValue(b.updated) - dateValue(a.updated));
    ui.count.textContent = `Сессий: ${filtered.length}${filtered.length !== sessions.length ? ` из ${sessions.length}` : ''}`;
    ui.listState.hidden = filtered.length > 0;
    if (!filtered.length) ui.listState.textContent = sessions.length ? 'По этим условиям ничего не найдено. Измените поиск или фильтры.' : 'Сессии не найдены. Проверьте состояние источников или обновите список.';
    for (const session of filtered.slice(0, visibleLimit)) {
      const li = node('li', 'session-item'); const b = button('', 'session-button', () => selectSession(session.id));
      if (session.id === state.id && state.view === 'session') b.setAttribute('aria-current', 'true');
      const sourceLabel = shown(session.source, 'Неизвестно') + (Number.isInteger(session.message_count) ? ` / ${session.message_count} записей` : '');
      const top = node('span', 'session-row'); top.append(node('span', 'session-title', shown(session.title, 'Сессия без названия')), node('span', 'session-source', sourceLabel));
      const meta = node('span', 'session-meta'); meta.append(node('span', '', shown(session.project, 'Проект не указан')), node('span', '', dateText(session.updated)));
      b.append(top, meta); li.append(b); ui.list.append(li);
    }
    ui.more.hidden = filtered.length <= visibleLimit;
    if (!ui.more.hidden) ui.more.textContent = `Показать ещё (${filtered.length - visibleLimit})`;
  }
  async function loadSessions() {
    const request = ++scanRequest; ui.refresh.disabled = true; ui.refresh.textContent = 'Обновление…';
    if (!sessions.length) { ui.count.textContent = 'Загрузка сессий…'; ui.listState.hidden = false; ui.listState.textContent = 'Загрузка сессий…'; }
    try {
      const data = await api('/api/sessions'); if (request !== scanRequest) return;
      if (!Array.isArray(data.sessions) || !Array.isArray(data.sources)) throw new Error('Неожиданный формат списка.');
      sessions = data.sessions; sources = data.sources;
      ui.scanTime.textContent = data.scanned_at ? `Сканирование: ${dateText(data.scanned_at)}` : '';
      updateControls(); renderNavigation(); renderList();
      if (state.id || state.view === 'files') loadContent();
    } catch (error) {
      if (request !== scanRequest) return;
      ui.listState.hidden = false; ui.listState.textContent = `Не удалось загрузить источники: ${error.message} Повторите обновление.`;
      if (!sessions.length) { ui.count.textContent = 'Сессии недоступны'; ui.sources.textContent = 'Сведения об источниках недоступны.'; }
    } finally { if (request === scanRequest) { ui.refresh.disabled = false; ui.refresh.textContent = 'Обновить источники'; } }
  }
  function detailSection(title) { const el = node('section', 'detail-section'); el.append(node('h3', '', title)); return el; }
  function notice(parent, text) { if (text) { const el = node('p', 'notice', text); el.setAttribute('role', 'note'); parent.append(el); } }
  function relationButton(session) {
    const b = button('', 'relation-button', () => selectSession(session.id));
    b.append(node('span', '', shown(session.title, 'Сессия без названия')), node('span', 'subtle', shown(session.source, 'Неизвестный источник'))); return b;
  }
  async function reveal(payload, result, control) {
    control.disabled = true; result.textContent = 'Открытие Finder…';
    try { const data = await api('/api/reveal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); if (!data.ok) throw new Error('Сервер не подтвердил открытие.'); result.textContent = 'Открыто в Finder.'; }
    catch (error) { result.textContent = `Не удалось открыть в Finder: ${error.message}`; }
    finally { control.disabled = false; }
  }
  function revealButton(payload, result) { const b = button('Показать в Finder', 'button', () => reveal(payload, result, b)); return b; }
  async function copyPath(path, result) {
    try { if (!navigator.clipboard?.writeText) throw new Error('Буфер обмена недоступен.'); await navigator.clipboard.writeText(path); result.textContent = 'Путь скопирован.'; }
    catch (error) { result.textContent = `Не удалось скопировать путь: ${error.message}`; }
  }
  function isRemote(source) { return source === 'Hermes VPS'; }
  function metadataField(dl, label, value) { if (!value) return; const field = node('div', 'metadata-field'); field.append(node('dt', '', label), node('dd', '', value)); dl.append(field); }

  // Markdown is constructed as DOM nodes, never parsed as HTML; links remain inert text.
  function inlineMarkdown(parent, text) {
    const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^\s)]+\))/g;
    let last = 0, match;
    while ((match = pattern.exec(text))) {
      parent.append(document.createTextNode(text.slice(last, match.index)));
      const token = match[0];
      if (token[0] === '`') parent.append(node('code', '', token.slice(1, -1)));
      else if (token.startsWith('**') || token.startsWith('__')) parent.append(node('strong', '', token.slice(2, -2)));
      else if (token[0] === '*' || token[0] === '_') parent.append(node('em', '', token.slice(1, -1)));
      else parent.append(document.createTextNode(token));
      last = pattern.lastIndex;
    }
    parent.append(document.createTextNode(text.slice(last)));
  }
  function markdown(text) {
    const body = node('div', 'message-body'); const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const fence = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
      if (fence) {
        const marker = fence[1], language = fence[2].trim(); const content = []; i++;
        while (i < lines.length && !new RegExp(`^\\s*${marker[0]}{${marker.length},}\\s*$`).test(lines[i])) content.push(lines[i++]);
        if (i < lines.length) i++;
        const block = node('div', 'code-block'); if (language) block.append(node('div', 'code-language', language));
        const pre = node('pre', ''); pre.append(node('code', '', content.join('\n'))); block.append(pre); body.append(block); continue;
      }
      const heading = line.match(/^\s{0,3}#{1,6}\s+(.+)$/);
      if (heading) { const h = node('p', 'markdown-heading'); inlineMarkdown(h, heading[1]); body.append(h); i++; continue; }
      const listMatch = line.match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
      if (listMatch) {
        const ordered = /^\d/.test(listMatch[1]), list = node(ordered ? 'ol' : 'ul', 'markdown-list');
        while (i < lines.length) { const m = lines[i].match(/^\s*([-+*]|\d+[.)])\s+(.+)$/); if (!m || /^\d/.test(m[1]) !== ordered) break; const li = node('li', ''); inlineMarkdown(li, m[2]); list.append(li); i++; }
        body.append(list); continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const quote = node('blockquote', ''); const parts = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) parts.push(lines[i++].replace(/^\s*>\s?/, ''));
        inlineMarkdown(quote, parts.join('\n')); body.append(quote); continue;
      }
      const parts = [line]; i++;
      while (i < lines.length && lines[i].trim() && !/^\s*(`{3,}|~{3,}|#{1,6}\s|>|[-+*]\s|\d+[.)]\s)/.test(lines[i])) parts.push(lines[i++]);
      const p = node('p', ''); inlineMarkdown(p, parts.join('\n')); body.append(p);
    }
    if (!body.childNodes.length) body.append(node('p', 'subtle', 'Текст не указан'));
    return body;
  }
  function appendMessages(stream, messages) {
    for (const m of messages) {
      const item = node('li', 'message'); const top = node('div', 'message-heading');
      const roles = { user: 'Пользователь', assistant: 'Ассистент', system: 'Система', tool: 'Инструмент', developer: 'Разработчик' };
      const identity = node('div', 'message-identity'); const role = String(m.role || '').toLowerCase();
      identity.append(node('span', 'message-role', roles[role] || shown(m.role, 'Неизвестная роль')));
      if (m.kind) identity.append(node('span', 'message-kind', m.kind)); top.append(identity);
      if (m.time) top.append(node('time', 'message-time', dateText(m.time)));
      const kind = String(m.kind || '').toLowerCase().replace(/[\s_./-]+/g, '');
      const tool = /^(?:tool(?:call|result|use|output)?|function(?:call|result|output))$/.test(kind) || /^(?:tool|toolresult)$/.test(role.replace(/[\s_./-]+/g, ''));
      const body = markdown(m.text);
      if (typeof m.image === 'string' && /^data:image\/(?:png|jpeg|gif|webp);base64,[a-z0-9+/]+={0,2}$/i.test(m.image)) {
        const image = node('img', 'preview-image'); image.alt = 'Вложенное изображение'; image.loading = 'lazy'; image.src = m.image; body.append(image);
      }
      if (tool) { const details = node('details', 'tool-record'); const summary = node('summary', 'tool-summary'); summary.append(top); details.append(summary, body); item.append(details); }
      else item.append(top, body);
      stream.append(item);
    }
  }
  function renderDetail(data, request) {
    const session = data.session;
    if (!session || !Array.isArray(data.messages) || !Array.isArray(data.files) || !Array.isArray(data.children)) throw new Error('Неожиданный формат сведений о сессии.');
    ui.detail.replaceChildren();
    const header = node('div', 'detail-header'); const titles = node('div', 'detail-title-group');
    const title = node('h2', '', shown(session.title, 'Сессия без названия')); title.id = 'detail-heading';
    titles.append(title, node('p', 'detail-context', `${shown(session.source, 'Неизвестный источник')} / ${shown(session.project, 'Проект не указан')}`));
    const actions = node('div', 'detail-actions'), actionMessage = node('p', 'action-message'); actionMessage.setAttribute('role', 'status');
    if (session.path) {
      actions.append(button('Скопировать путь', 'button', () => copyPath(session.path, actionMessage)));
      if (!isRemote(session.source)) actions.append(revealButton({ id: session.id }, actionMessage));
    }
    header.append(titles, actions); ui.detail.append(header, actionMessage);
    notice(ui.detail, data.notice);
    const information = node('details', 'information'); const summary = node('summary', '', 'Сведения и связи'); information.append(summary);
    const dl = node('dl', 'metadata'); metadataField(dl, 'ID сессии', session.id); metadataField(dl, 'Путь к сессии', session.path); metadataField(dl, 'Обновлено', session.updated ? dateText(session.updated) : ''); metadataField(dl, 'Модель', session.model); metadataField(dl, 'Записанный статус', session.status); information.append(dl);
    if (data.parent || data.children.length || session.parent_id) {
      const relations = detailSection('Подтверждённые связи');
      if (data.parent) { relations.append(node('p', 'relationship-label', 'Родительская сессия'), relationButton(data.parent)); }
      else if (session.parent_id) relations.append(node('p', 'subtle', `Родительская сессия недоступна: ${session.parent_id}`));
      if (data.children.length) { relations.append(node('p', 'relationship-label', 'Дочерние сессии')); for (const child of data.children) relations.append(relationButton(child)); }
      information.append(relations);
    }
    const related = session.project ? sessions.filter(s => s.id !== session.id && projectKey(s) === projectKey(session)).sort((a, b) => dateValue(b.updated) - dateValue(a.updated)) : [];
    if (related.length) {
      const group = detailSection('Сессии с тем же проектом'); group.append(node('p', 'subtle', 'Совпадение пути проекта на одной машине; прямая связь между сессиями не предполагается.'));
      let shownCount = 0;
      const showRelated = button('Показать ещё сессии проекта', 'text-button', () => appendRelated());
      function appendRelated() {
        for (const item of related.slice(shownCount, shownCount + 20)) group.insertBefore(relationButton(item), showRelated);
        shownCount += 20; showRelated.hidden = shownCount >= related.length;
      }
      group.append(showRelated); appendRelated(); information.append(group);
    }
    ui.detail.append(information);
    const files = node('details', 'information attachment-disclosure');
    const filesSummary = node('summary', '');
    const seenFiles = new Set();
    files.hidden = true; files.append(filesSummary); ui.detail.append(files);
    function appendFiles(items) {
      for (const file of items) {
        const key = file.relative || file.path || file.label;
        if (key && seenFiles.has(key)) continue;
        if (key) seenFiles.add(key);
        const row = node('div', 'file-row'); const text = node('div', 'file-content'); text.append(node('strong', '', shown(file.label, 'Файл')), node('span', 'file-path', shown(file.path, 'Путь недоступен'))); row.append(text);
        if (typeof file.relative === 'string' && file.relative) row.append(button('Открыть', 'text-button', () => openFiles(session.source, file.relative, true)));
        else row.append(node('span', 'subtle', 'Просмотр недоступен'));
        files.append(row);
      }
      const count = files.children.length - 1;
      files.hidden = count === 0;
      filesSummary.textContent = `Файлы сессии (${count})`;
    }
    appendFiles(data.files);
    const conversation = detailSection('История'); const stream = node('ol', 'message-stream');
    if (!data.messages.length && !data.has_more) conversation.append(node('p', 'state', 'Доступных сообщений или вызовов инструментов нет.'));
    appendMessages(stream, data.messages); conversation.append(stream);
    let next = data.next_offset; const status = node('p', 'page-status'); status.setAttribute('role', 'status');
    const more = button('Загрузить следующие сообщения', 'button', async () => {
      more.disabled = true; status.textContent = 'Загрузка сообщений…';
      try {
        const page = await api(`/api/session?id=${encodeURIComponent(session.id)}&offset=${encodeURIComponent(next)}`);
        if (request !== contentRequest) return;
        if (!Array.isArray(page.messages) || !Array.isArray(page.files)) throw new Error('Неожиданный формат страницы.');
        if (page.has_more && (page.next_offset === null || page.next_offset === undefined || page.next_offset === next)) throw new Error('Сервер не вернул следующую страницу.');
        appendMessages(stream, page.messages); appendFiles(page.files); if (page.notice) notice(conversation, page.notice); next = page.next_offset;
        more.hidden = !page.has_more; status.textContent = page.has_more ? '' : 'История загружена до конца.';
      } catch (error) { if (request === contentRequest) status.textContent = `Не удалось загрузить сообщения: ${error.message} Повторите попытку.`; }
      finally { more.disabled = false; }
    });
    more.hidden = !data.has_more; conversation.append(more, status); ui.detail.append(conversation);
  }
  function crumbBar(source, path, file) {
    const bar = node('nav', 'breadcrumbs'); bar.setAttribute('aria-label', 'Путь к файлу');
    bar.append(button(source, 'crumb', () => openFiles(source)));
    const parts = path.split('/').filter(Boolean);
    parts.forEach((part, i) => { const sep = node('span', 'crumb-separator', '/'); sep.setAttribute('aria-hidden', 'true'); bar.append(sep); const p = parts.slice(0, i + 1).join('/'); if (i === parts.length - 1) bar.append(node('span', 'crumb-current', part)); else bar.append(button(part, 'crumb', () => openFiles(source, p))); });
    return bar;
  }
  function directoryParent(path) { const parts = path.split('/').filter(Boolean); parts.pop(); return parts.join('/'); }
  async function renderDirectory(source, path, request) {
    const fetchPage = offset => api(`/api/files?source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}&offset=${encodeURIComponent(offset)}`);
    const data = await fetchPage(0);
    if (request !== contentRequest) return;
    if (!Array.isArray(data.entries)) throw new Error('Неожиданный формат каталога.');
    ui.detail.replaceChildren(); const heading = node('div', 'detail-header'); const title = node('h2', '', 'Файлы источника'); title.id = 'detail-heading'; heading.append(title);
    const actions = node('div', 'detail-actions'); const result = node('p', 'action-message'); result.setAttribute('role', 'status');
    if (!isRemote(source)) actions.append(revealButton({ source, path }, result)); heading.append(actions); ui.detail.append(heading, crumbBar(source, path, false), result);
    if (data.root) ui.detail.append(node('p', 'root-path', data.root)); notice(ui.detail, data.notice);
    if (path) ui.detail.append(button('В родительскую папку', 'text-button back-directory', () => openFiles(source, directoryParent(path))));
    const listing = node('div', 'directory-list');
    if (!data.entries.length && (data.next_offset === null || data.next_offset === undefined)) listing.append(node('p', 'state', 'В этой папке нет доступных записей.'));
    function appendEntries(entries) {
      for (const entry of entries) {
        const row = node('div', 'directory-row'); const info = node('div', 'directory-info');
        const canOpen = !entry.restricted && (entry.kind === 'file' || entry.kind === 'directory');
        const name = canOpen ? button(shown(entry.name, 'Без имени'), 'file-name', () => openFiles(source, entry.path, entry.kind === 'file')) : node('span', 'file-name unavailable', shown(entry.name, 'Без имени'));
        info.append(name);
        const kinds = { directory: 'Папка', file: 'Файл', symlink: 'Символическая ссылка', other: 'Другая запись' };
        const details = [kinds[entry.kind] || 'Неизвестная запись']; if (entry.restricted) details.push('просмотр ограничен'); else if (!canOpen) details.push('просмотр недоступен');
        if (entry.size !== undefined && entry.size !== null && entry.kind === 'file') details.push(`${Number(entry.size).toLocaleString('ru-RU')} байт`);
        info.append(node('span', 'subtle', details.join(' / '))); row.append(info);
        if (entry.modified) row.append(node('span', 'subtle file-date', dateText(entry.modified)));
        listing.append(row);
      }
    }
    appendEntries(data.entries); ui.detail.append(listing);
    let next = data.next_offset;
    const status = node('p', 'page-status'); status.setAttribute('role', 'status');
    const more = button('Показать ещё файлы', 'button', async () => {
      more.disabled = true; status.textContent = 'Загрузка каталога…';
      try {
        const page = await fetchPage(next); if (request !== contentRequest) return;
        if (!Array.isArray(page.entries)) throw new Error('Неожиданный формат каталога.');
        if (page.next_offset !== null && page.next_offset !== undefined && page.next_offset === next) throw new Error('Сервер не вернул следующую страницу.');
        appendEntries(page.entries); notice(ui.detail, page.notice); next = page.next_offset;
        more.hidden = next === null || next === undefined; status.textContent = more.hidden ? 'Каталог загружен до конца.' : '';
      } catch (error) { if (request === contentRequest) status.textContent = `Не удалось загрузить каталог: ${error.message} Повторите попытку.`; }
      finally { more.disabled = false; }
    });
    more.hidden = next === null || next === undefined; ui.detail.append(more, status);
  }
  async function renderFile(source, path, request) {
    const fetchPage = offset => api(`/api/file?source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}&offset=${encodeURIComponent(offset)}`);
    const data = await fetchPage(0); if (request !== contentRequest) return;
    ui.detail.replaceChildren(); const header = node('div', 'detail-header'); const title = node('h2', '', shown(data.name, path.split('/').pop() || 'Файл')); title.id = 'detail-heading';
    const actions = node('div', 'detail-actions'); const result = node('p', 'action-message'); result.setAttribute('role', 'status');
    if (!isRemote(source)) actions.append(revealButton({ source, path }, result)); header.append(title, actions);
    ui.detail.append(header, crumbBar(source, path, true), result, button('В папку', 'text-button back-directory', () => openFiles(source, directoryParent(path)))); notice(ui.detail, data.notice);
    if (data.kind === 'image') {
      if (typeof data.data_url === 'string' && /^data:image\/(?:png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(data.data_url)) {
        const image = node('img', 'preview-image'); image.alt = shown(data.name, 'Изображение'); image.src = data.data_url; ui.detail.append(image);
      } else ui.detail.append(node('p', 'state', 'Изображение недоступно для безопасного просмотра.'));
    } else if (data.kind === 'text') {
      const pre = node('pre', 'file-preview', shown(data.text, '')); ui.detail.append(pre); let next = data.next_offset;
      const status = node('p', 'page-status'); status.setAttribute('role', 'status');
      const more = button('Загрузить продолжение файла', 'button', async () => {
        more.disabled = true; status.textContent = 'Загрузка файла…';
        try {
          const page = await fetchPage(next); if (request !== contentRequest) return;
          if (page.kind !== 'text' || typeof page.text !== 'string') throw new Error('Неожиданный формат продолжения.');
          if (page.next_offset !== null && page.next_offset !== undefined && page.next_offset === next) throw new Error('Сервер не вернул следующую часть.');
          pre.append(document.createTextNode(page.text)); next = page.next_offset; notice(ui.detail, page.notice); more.hidden = next === null || next === undefined; status.textContent = more.hidden ? 'Файл загружен до конца.' : '';
        } catch (error) { if (request === contentRequest) status.textContent = `Не удалось загрузить продолжение: ${error.message} Повторите попытку.`; }
        finally { more.disabled = false; }
      }); more.hidden = next === null || next === undefined; ui.detail.append(more, status);
    } else ui.detail.append(node('p', 'state', 'Этот формат недоступен для просмотра.'));
  }
  async function loadContent() {
    const request = ++contentRequest;
    if (state.view !== 'files' && !state.id) { ui.detail.replaceChildren(message('Выберите сессию', 'Здесь появится история, файлы и подтверждённые связи.')); return; }
    ui.detail.replaceChildren(message(state.view === 'files' ? 'Загрузка файлов…' : 'Загрузка сессии…', 'Чтение данных источника.'));
    try {
      if (state.view === 'files') {
        if (!state.fileSource) throw new Error('Источник не указан.');
        if (state.file) await renderFile(state.fileSource, state.path, request); else await renderDirectory(state.fileSource, state.path, request);
      } else { const data = await api(`/api/session?id=${encodeURIComponent(state.id)}&offset=0`); if (request === contentRequest) renderDetail(data, request); }
    } catch (error) { if (request === contentRequest) ui.detail.replaceChildren(message('Не удалось открыть данные', `${error.message} Проверьте источник и повторите попытку.`, true)); }
  }
  ui.refresh.addEventListener('click', loadSessions);
  ui.search.addEventListener('input', () => { state.q = ui.search.value; visibleLimit = pageSize; renderList(); clearTimeout(searchTimer); searchTimer = setTimeout(() => writeUrl(), 300); });
  ui.source.addEventListener('change', () => setFilter('source', ui.source.value));
  ui.project.addEventListener('change', () => setFilter('project', ui.project.value));
  ui.messageFilter.addEventListener('change', () => setFilter('hasMessages', ui.messageFilter.checked));
  ui.more.addEventListener('click', () => { visibleLimit += pageSize; renderList(); });
  window.addEventListener('popstate', () => { clearTimeout(searchTimer); state = readUrl(); visibleLimit = pageSize; updateControls(); renderNavigation(); renderList(); loadContent(); });
  if (window.matchMedia('(max-width: 1000px)').matches) document.querySelector('.navigation').open = false;
  updateControls(); renderNavigation(); if (state.id || state.view === 'files') loadContent(); loadSessions();
})();
