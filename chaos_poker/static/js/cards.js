// Playing cards drawn with HTML/CSS only (section 11.4). Card format: "As", "Td"; null = face down.
(() => {
  const suits = {
    s: { symbol: '♠', name: 'пик', color: 'black' },
    h: { symbol: '♥', name: 'червей', color: 'red' },
    d: { symbol: '♦', name: 'бубен', color: 'blue' },
    c: { symbol: '♣', name: 'треф', color: 'green' },
  };
  const ranks = { T: '10' };
  const rankNames = { J: 'Валет', Q: 'Дама', K: 'Король', A: 'Туз', T: '10' };
  const faces = new Set(['J', 'Q', 'K', 'A']);

  // mark: 'joker' (gold frame + ★), 'spy' (highlight + caption), 'blind' (face down with "?").
  window.renderCard = (value, options = {}) => {
    // Classic two-color deck by default; four colors only when the lobby setting is on (11.4).
    const { faceDown = false, fourColor = false, highlight = false, mark = null } = options;
    const card = document.createElement('span');
    card.className = 'playing-card';
    if (highlight) card.classList.add('playing-card--highlight');
    if (faceDown || !value) {
      card.classList.add('playing-card--back');
      if (mark === 'blind') card.classList.add('playing-card--blind');
      card.setAttribute('aria-label', mark === 'blind' ? 'Слепая карта флопа' : 'Скрытая карта');
      return card;
    }

    const rank = value[0];
    const suit = suits[value[1]];
    if (!suit) {
      card.textContent = value;
      return card;
    }
    card.classList.add(`playing-card--${fourColor ? suit.color : (suit.color === 'blue' || suit.color === 'green' ? 'black' : suit.color)}`);
    const visibleRank = ranks[rank] || rank;
    let label = `${rankNames[rank] || visibleRank} ${suit.name}`;
    if (mark === 'joker') {
      card.classList.add('playing-card--joker');
      label += ', джокер';
    }
    if (mark === 'spy') {
      card.classList.add('playing-card--spy');
      label += ', открыта шпионом';
    }
    card.setAttribute('aria-label', label);
    // Face cards have no portraits: a big letter with the suit in the center.
    const center = faces.has(rank)
      ? `<span class="playing-card__center playing-card__center--face">${visibleRank}<small>${suit.symbol}</small></span>`
      : `<span class="playing-card__center">${suit.symbol}</span>`;
    card.innerHTML = `
      <span class="playing-card__corner"><span>${visibleRank}</span><span class="playing-card__suit">${suit.symbol}</span></span>
      ${center}
      <span class="playing-card__corner playing-card__corner--bottom"><span>${visibleRank}</span><span class="playing-card__suit">${suit.symbol}</span></span>
      ${mark === 'joker' ? '<span class="playing-card__badge">★</span>' : ''}
      ${mark === 'spy' ? '<span class="playing-card__caption">Шпион</span>' : ''}`;
    return card;
  };

  // marks: optional array of per-card marks (same length as values); highlight: optional Set of card values.
  window.renderCardList = (values, options = {}, marks = [], highlight = null) => {
    const fragment = document.createDocumentFragment();
    values.forEach((value, index) => fragment.appendChild(window.renderCard(value, {
      ...options, mark: marks[index] || null, highlight: Boolean(highlight?.has(value)),
    })));
    return fragment;
  };

  // Cards of the winning combinations of a finished hand (last_hand.best_cards).
  window.winningCards = hand => new Set(Object.values(hand?.best_cards || {}).flat());

  // Re-render a row of cards, animating only what changed (11.4):
  // new card -> "deal" (flies in) or "flip" (rotateY, 200 ms apart, used for the board).
  window.renderCardsInto = (container, values, options = {}, marks = [], { flip = false, highlight = null } = {}) => {
    const key = JSON.stringify([values, marks]);
    if (container.dataset.cards === key && !highlight) return;
    const previous = JSON.parse(container.dataset.values || '[]');
    container.dataset.cards = key;
    container.dataset.values = JSON.stringify(values);
    const fragment = document.createDocumentFragment();
    let delay = 0;
    values.forEach((value, index) => {
      const card = window.renderCard(value, { ...options, mark: marks[index] || null, highlight: highlight?.has(value) });
      if (previous[index] !== value) {
        card.classList.add(flip ? 'card-flip' : 'card-deal');
        card.style.animationDelay = `${delay}ms`;
        delay += flip ? 200 : 120;
      }
      fragment.appendChild(card);
    });
    container.replaceChildren(fragment);
  };

  // Board marks for the current event: blind flop and the revealed joker.
  window.boardMarks = (board, event) => board.map((value, index) => {
    if (value === null && event?.id === 'blind_flop') return 'blind';
    if (event?.joker && event.joker.index === index) return 'joker';
    return null;
  });

  // Event / ability card: framed card with an emoji icon, title and short description.
  window.renderEventCard = (event, { kind = 'event' } = {}) => {
    const box = document.createElement('div');
    box.className = `chaos-card chaos-card--${kind}`;
    const icon = document.createElement('span');
    icon.className = 'chaos-card__icon';
    icon.textContent = event.icon || '⚡';
    const title = document.createElement('strong');
    title.textContent = event.title;
    const text = document.createElement('span');
    text.className = 'chaos-card__text';
    text.textContent = event.description || '';
    box.append(icon, title, text);
    return box;
  };

  // Light screen shake when an event card appears (<= 500 ms, CSS only).
  window.shakeScreen = () => {
    document.body.classList.remove('shake');
    void document.body.offsetWidth;
    document.body.classList.add('shake');
  };
})();
