// Shared client helpers: HTML escaping, WebSocket with auto-reconnect, toasts.
(() => {
  const escapeMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  // Every user-provided string (names, task texts) must go through esc() before innerHTML.
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => escapeMap[char]);

  const STREETS = { preflop: 'Префлоп', flop: 'Флоп', turn: 'Тёрн', river: 'Ривер', showdown: 'Шоудаун', complete: 'Раздача окончена' };
  const STATUSES = { active: '', folded: 'фолд', all_in: 'олл-ин', out: 'выбыл', waiting: '' };

  const ERRORS = {
    "It is not this player's turn": 'Сейчас не ваш ход',
    'Invalid raise amount': 'Недопустимая сумма рейза',
    'Game is paused': 'Игра на паузе',
    'Only one ability per betting round': 'Не больше одной способности за круг торгов',
    'Frozen players cannot raise': 'Вы заморожены: рейз запрещён',
    'Too many messages': 'Слишком много действий, подождите',
    'Betting is not open': 'Торги ещё не начались',
    'Veto is only possible in the veto window': 'Вето можно применить только в окне вето',
    'Abilities can only be used during a hand': 'Способности доступны только во время раздачи',
    'No carousel in progress': 'Карусель уже завершилась',
  };

  function toast(text, kind = 'info') {
    let box = document.querySelector('#toasts');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toasts';
      box.style.cssText = 'position:fixed;left:50%;bottom:16px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:50;width:min(92vw,420px)';
      document.body.appendChild(box);
    }
    const item = document.createElement('div');
    const colors = { info: '#1e293b', error: '#7f1d1d', private: '#312e81' };
    item.style.cssText = `background:${colors[kind] || colors.info};color:#f8fafc;padding:12px 14px;border-radius:10px;box-shadow:0 6px 20px rgb(0 0 0/40%);font:15px system-ui,sans-serif`;
    item.textContent = text;
    box.appendChild(item);
    setTimeout(() => item.remove(), kind === 'private' ? 8000 : 4000);
  }

  // Opens /ws for a session token and reconnects after drops. Returns { send(type, payload) }.
  function connect(token, onMessage, { onUnauthorized } = {}) {
    let socket = null;
    let msgId = 0;
    let stopped = false;
    const open = () => {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
      socket = new WebSocket(`${protocol}://${location.host}/ws?token=${encodeURIComponent(token)}`);
      socket.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.type === 'error' && message.payload?.code === 'unauthorized') {
          stopped = true;
          onUnauthorized?.();
          return;
        }
        if (message.type === 'error') {
          sound('error');
          toast(ERRORS[message.payload?.message] || message.payload?.message || 'Ошибка', 'error');
          return;
        }
        onMessage(message);
      };
      socket.onclose = () => { if (!stopped) setTimeout(open, 1500); };
    };
    open();
    return {
      send(type, payload = {}) {
        if (!socket || socket.readyState !== WebSocket.OPEN) {
          toast('Соединение восстанавливается…', 'error');
          return;
        }
        socket.send(JSON.stringify({ type, payload, msg_id: ++msgId }));
      },
    };
  }

  // Local countdown: the server sends seconds left only when the state changes.
  // Usage: <span data-countdown="12"></span> — set via setCountdown(el, seconds).
  function setCountdown(element, seconds) {
    if (seconds === null || seconds === undefined) {
      delete element.dataset.deadline;
      element.textContent = '';
      return;
    }
    element.dataset.deadline = String(Date.now() + seconds * 1000);
    element.textContent = String(seconds);
  }
  setInterval(() => {
    document.querySelectorAll('[data-deadline]').forEach(element => {
      element.textContent = String(Math.max(0, Math.ceil((Number(element.dataset.deadline) - Date.now()) / 1000)));
    });
  }, 250);

  // Final results (section 13): standings, the judge's protocol with nominations, statistics.
  function resultsHtml(results) {
    const standings = '<ol>' + results.standings
      .map(item => `<li><strong>${esc(item.name)}</strong> — ${item.stack}</li>`).join('') + '</ol>';
    const protocol = results.protocol;
    const protocolHtml = protocol
      ? `<h3>📜 Протокол вечера${protocol.source === 'ai' ? ' от судьи-ИИ' : ''}</h3><p>${esc(protocol.summary)}</p><ul>`
        + protocol.nominations.map(item => `<li>🏅 <strong>${esc(item.title)}</strong>: ${esc(item.player)} — ${esc(item.reason)}</li>`).join('')
        + '</ul>'
      : '';
    const rows = (results.stats || []).map(row => `<tr><td>${esc(row.name)}</td><td>${row.hands_won}/${row.hands_played}</td>
      <td>${row.biggest_pot}</td><td>${row.bluffs}</td><td>${row.abilities_used}</td><td>${row.tasks_done}</td></tr>`).join('');
    const stats = rows
      ? `<h3>Статистика</h3><div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%">
          <tr><th align="left">Игрок</th><th>Победы</th><th>Макс. банк</th><th>Блефы</th><th>Способности</th><th>Задания</th></tr>${rows}</table></div>`
      : '';
    return `<h2>Игра окончена · раздач: ${results.hands_played}</h2>${standings}${protocolHtml}${stats}`;
  }

  // Short synthesized sounds (WebAudio, no files). On by default, remembered per device.
  const SOUNDS = {
    turn: [[660, 0, 0.12], [880, 0.14, 0.14]],
    event: [[220, 0, 0.18], [330, 0.12, 0.18], [440, 0.24, 0.25]],
    win: [[523, 0, 0.12], [659, 0.12, 0.12], [784, 0.24, 0.12], [1047, 0.36, 0.25]],
    task: [[440, 0, 0.1], [440, 0.16, 0.1]],
    error: [[180, 0, 0.25]],
  };
  let audio = null;
  const soundOn = () => {
    try { return localStorage.getItem('chaos_sound') !== '0'; } catch { return true; }
  };
  function sound(name) {
    if (!soundOn() || !SOUNDS[name]) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === 'suspended') audio.resume();
      const start = audio.currentTime;
      SOUNDS[name].forEach(([frequency, offset, length]) => {
        const oscillator = audio.createOscillator();
        const gain = audio.createGain();
        oscillator.type = 'triangle';
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.0001, start + offset);
        gain.gain.exponentialRampToValueAtTime(0.18, start + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + length);
        oscillator.connect(gain).connect(audio.destination);
        oscillator.start(start + offset);
        oscillator.stop(start + offset + length + 0.05);
      });
    } catch { /* no audio on this device */ }
  }
  // A toggle button: <button data-sound-toggle></button>
  function bindSoundToggle(button) {
    const paint = () => { button.textContent = soundOn() ? '🔊 Звук' : '🔇 Звук'; };
    button.addEventListener('click', () => {
      try { localStorage.setItem('chaos_sound', soundOn() ? '0' : '1'); } catch { /* ignore */ }
      paint();
      sound('task');
    });
    paint();
  }

  window.Chaos = { esc, toast, connect, setCountdown, resultsHtml, sound, bindSoundToggle, STREETS, STATUSES };
})();
