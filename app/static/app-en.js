'use strict';

const $ = id => document.getElementById(id);

const state = {
  symbol: 'BTCUSDT',
  interval: '1h',
  horizon: 0,
  offset: 0,
  version: 0,
  busy: false,
  marketGood: false,
  lastChartKey: '',
  errors: {}
};

const number = value => Number(value).toLocaleString('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2
});

const signed = value =>
  `${value >= 0 ? '+' : ''}${Number(value).toFixed(2)}%`;

const timeText = value =>
  new Date(value).toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit'
  });

const dateText = value =>
  new Date(value).toLocaleString('en-GB', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });

const escapeHTML = text =>
  String(text ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[c]));

const displaySymbol = symbol => {
  if (symbol.endsWith('USDT')) {
    return symbol.slice(0, -4) + ' / USDT';
  }
  return symbol;
};

const horizonText = horizon => {
  if (horizon === 30) return '30 minutes';
  if (horizon === 1) return '1 hour';
  return '4 hours';
};

const chart = new CandleChart($('chart'), c => {
  $('ohlc').textContent =
    `${dateText(c.time)}   O ${number(c.open)}   H ${number(c.high)}   ` +
    `L ${number(c.low)}   C ${number(c.close)}` +
    `${c.close_time > Date.now() ? ' · forming' : ''}`;
});

async function api(path) {
  const response = await fetch(path, {
    signal: AbortSignal.timeout(25000),
    cache: 'no-store'
  });

  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {}

    throw new Error(
      typeof detail === 'string'
        ? detail
        : `Service temporarily unavailable (${response.status})`
    );
  }

  return response.json();
}

function updateMarketCard(prefix, ticker) {
  $(prefix + '-price').textContent = '$' + number(ticker.price);
  $(prefix + '-change').textContent = signed(ticker.change_pct);
  $(prefix + '-change').className =
    'market-change ' +
    (ticker.change_pct >= 0 ? 'positive' : 'negative');
}

function updateQuote(ticker) {
  if (ticker.symbol === 'BTCUSDT') {
    updateMarketCard('btc', ticker);
  }

  if (ticker.symbol === 'ETHUSDT') {
    updateMarketCard('eth', ticker);
  }

  if (ticker.symbol === state.symbol) {
    $('main-price').textContent = number(ticker.price);
    $('main-change').textContent = signed(ticker.change_pct);
    $('main-change').className =
      ticker.change_pct >= 0 ? 'positive' : 'negative';
    $('updated').textContent = 'Price · ' + timeText(ticker.updated_at);
  }
}

function updateSelectedSymbolUI() {
  $('chart-title').innerHTML =
    `${displaySymbol(state.symbol)} ` +
    `<span class="exchange-label">BINANCE</span>`;

  $('export').href =
    '/api/export.csv?symbol=' + encodeURIComponent(state.symbol);

  document.querySelectorAll('[data-symbol]').forEach(button => {
    const selected = button.dataset.symbol === state.symbol;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', selected);
  });
}

function renderError() {
  const messages = [
    ...new Set(Object.values(state.errors).filter(Boolean))
  ];

  $('error').textContent = messages.join(' ');
  $('error').classList.toggle('hidden', !messages.length);

  $('connection-dot').className =
    'status-dot ' + (state.marketGood ? 'online' : 'offline');

  $('connection-text').textContent = state.marketGood
    ? (messages.length
        ? 'Binance data · some issues'
        : 'Binance · public data')
    : 'Data unavailable or loading';
}

function renderForecasts(data) {
  if (!data.items.length) {
    $('forecast-cards').innerHTML =
      '<div class="empty compact">' +
      'Waiting for the first calculation.<br>' +
      'No forecast is created when data is unavailable.' +
      '</div>';
    return;
  }

  $('forecast-cards').innerHTML = data.items.map(p => {
    const kind =
      p.direction === 'UP'
        ? 'positive'
        : p.direction === 'DOWN'
          ? 'negative'
          : '';

    const icon =
      p.direction === 'UP'
        ? '↗'
        : p.direction === 'DOWN'
          ? '↘'
          : '→';

    const label =
      p.direction === 'UP'
        ? 'Rise'
        : p.direction === 'DOWN'
          ? 'Decline'
          : 'Watching';

    const stale = Date.now() - p.issued_at > 17 * 60000;

    const status =
      Date.now() >= p.due_at
        ? 'Archived · horizon completed'
        : stale
          ? 'Archived · no new calculation'
          : `Check at ${timeText(p.due_at)}`;

    const segments = Array.from(
      {length: 10},
      (_, i) =>
        `<i class="${(i + 1) * 10 <= p.confidence ? 'filled' : ''}"></i>`
    ).join('');

    return `
      <article class="forecast-card">
        <div class="forecast-top">
          <span class="horizon-label">
            For ${horizonText(p.horizon)}
          </span>
          <span class="forecast-target">${status}</span>
        </div>

        <div class="forecast-direction ${kind}">
          <span class="direction-icon">${icon}</span>
          <strong class="direction-label">${p.direction}</strong>
          <span class="direction-detail">${label}</span>
        </div>

        <div class="signal-strength">
          <span>Signal strength</span>
          <strong>${p.confidence} / 100</strong>
        </div>

        <div class="strength-track ${p.direction.toLowerCase()}">
          ${segments}
        </div>

        <p class="reasons">
          ${escapeHTML(p.reasons[0])} · RSI ${p.indicators.rsi14.toFixed(0)}
          <br>
          Neutral zone ±${p.neutral_pct.toFixed(2)}%
          <br>
          Calculated ${timeText(p.issued_at)} · reference ${timeText(p.reference_at)}
        </p>
      </article>
    `;
  }).join('');
}

function renderStats(data) {
  let total = 0;
  let evaluated = 0;
  let waits = 0;
  let waitCorrect = 0;

  for (const h of [30, 1, 4]) {
    const s = data.items.find(x => x.horizon === h);

    $('accuracy-' + h).textContent =
      s?.directional_accuracy == null
        ? '—'
        : s.directional_accuracy.toFixed(1) + '%';

    $('sample-' + h).textContent =
      s?.directional_total
        ? `${s.directional_correct} of ${s.directional_total} directional forecasts`
        : 'No completed UP / DOWN forecasts';

    if (s) {
      total += s.total;
      evaluated += s.evaluated;
      waits += s.wait_total;
      waitCorrect += s.wait_correct;
    }
  }

  $('wait-score').textContent =
    waits
      ? (100 * waitCorrect / waits).toFixed(1) + '%'
      : '—';

  $('wait-sample').textContent =
    waits
      ? `${waitCorrect} of ${waits} — inside neutral zone`
      : 'No completed WAIT forecasts';

  $('evaluated-total').textContent = evaluated;

  $('pending-total').textContent =
    total
      ? `${total - evaluated} awaiting evaluation · ${total} created`
      : 'History will start with the first forecast';
}

function renderHistory(data) {
  $('history-body').innerHTML = data.items.length
    ? data.items.map(p => {
        const done = p.evaluated_at != null;

        const status = done
          ? (p.correct ? 'Confirmed' : 'Not confirmed')
          : (p.due_at < Date.now()
              ? 'Awaiting data'
              : 'Awaiting evaluation');

        const note = done
          ? `Checked ${dateText(p.evaluated_at)}`
          : p.evaluation_note ||
            `Target time ${dateText(p.due_at)}`;

        return `
          <tr>
            <td>
              ${dateText(p.issued_at)}
              <small>
                Reference ${timeText(p.reference_at)} ·
                ${escapeHTML(p.model_version)}
              </small>
            </td>

            <td>
              ${horizonText(p.horizon)}
              <small>Until ${dateText(p.due_at)}</small>
            </td>

            <td>
              <span class="badge ${p.direction}">
                ${p.direction === 'UP'
                  ? '↗'
                  : p.direction === 'DOWN'
                    ? '↘'
                    : '→'}
                ${p.direction}
              </span>
              <small>Strength ${p.confidence}/100</small>
            </td>

            <td>
              $${number(p.base_price)}
              <small>Threshold ±${p.neutral_pct.toFixed(2)}%</small>
            </td>

            <td>
              ${
                done
                  ? `$${number(p.actual_price)}
                     <small class="${p.change_pct >= 0 ? 'positive' : 'negative'}">
                       ${signed(p.change_pct)} · ${p.observed_direction}
                     </small>`
                  : '—'
              }
            </td>

            <td title="${escapeHTML(note)}">
              <span class="result-dot ${
                done
                  ? (p.correct ? 'win' : 'loss')
                  : ''
              }"></span>
              ${status}
              <small>
                ${done
                  ? 'Actual movement'
                  : 'Checked using 1m close'}
              </small>
            </td>
          </tr>
        `;
      }).join('')
    : `
      <tr>
        <td colspan="6">
          <div class="empty">
            Real forecasts and their evaluation results will appear here.
            <br>
            The first result appears after the 30-minute horizon is completed.
          </div>
        </td>
      </tr>
    `;

  $('history-count').textContent =
    data.total
      ? `${state.offset + 1}–${Math.min(state.offset + 20, data.total)} of ${data.total} records`
      : '0 records';

  $('previous').disabled = state.offset === 0;
  $('next').disabled = state.offset + 20 >= data.total;
}

function renderStatus(data) {
  state.errors.worker = Object.values(data.errors).join(' ');

  const notice =
    data.storage === 'sqlite'
      ? 'Local run: history is stored in data/crypto.db on this computer. PostgreSQL is configured for Render.'
      : data.render_free
        ? "On Render's free tier the server may sleep. After waking, saved forecasts are evaluated; missed forecasts are not backfilled."
        : '';

  $('storage-notice').textContent = notice;
  $('storage-notice').classList.toggle('hidden', !notice);
}

async function refreshAll() {
  const version = state.version;
  const symbol = state.symbol;

  if (state.busy === version + 1) return;

  state.busy = version + 1;
  $('refresh-icon').classList.add('spin');

  const params = `symbol=${encodeURIComponent(symbol)}`;
  const chartKey = symbol + state.interval;

  const tasks = [
    [
      'market',
      `/api/market?${params}&interval=${state.interval}`,
      data => {
        updateQuote(data.ticker);
        chart.setData(
          data.candles,
          state.lastChartKey !== chartKey
        );
        state.lastChartKey = chartKey;
        state.marketGood = true;
        $('chart-message').classList.add('hidden');
      }
    ],

    ['btcquote', '/api/quote?symbol=BTCUSDT', updateQuote],
    ['ethquote', '/api/quote?symbol=ETHUSDT', updateQuote],

    [
      'forecasts',
      `/api/forecasts?${params}`,
      renderForecasts
    ],

    [
      'history',
      `/api/history?${params}&horizon=${state.horizon}&offset=${state.offset}`,
      renderHistory
    ],

    [
      'stats',
      `/api/stats?${params}`,
      renderStats
    ],

    [
      'status',
      '/api/status',
      renderStatus
    ]
  ];

  const results = await Promise.allSettled(
    tasks.map(t => api(t[1]))
  );

  if (state.version !== version) return;

  results.forEach((result, index) => {
    const [key, , render] = tasks[index];

    if (result.status === 'fulfilled') {
      delete state.errors[key];
      render(result.value);
    } else {
      state.errors[key] =
        result.reason.name === 'TimeoutError'
          ? 'The server is responding slowly. We will retry automatically.'
          : result.reason.message;

      if (key === 'market') {
        state.marketGood = false;
        $('chart-message').textContent =
          'Could not update the chart. The latest loaded candles may be stale.';
        $('chart-message').classList.remove('hidden');
      }

      if (key === 'history') {
        $('history-body').innerHTML =
          '<tr><td colspan="6"><div class="empty">The forecast log is temporarily unavailable. We will retry automatically.</div></td></tr>';
      }

      if (key === 'forecasts') {
        $('forecast-cards').innerHTML =
          '<div class="empty compact">Forecasts are temporarily unavailable.<br>We will retry automatically.</div>';
      }
    }
  });

  renderError();
  state.busy = false;
  $('refresh-icon').classList.remove('spin');
}

function changeView() {
  state.version++;
  state.busy = false;
  refreshAll();
}

function selectSymbol(symbol) {
  state.symbol = symbol;
  state.offset = 0;
  state.marketGood = false;
  state.errors = {};

  chart.setData([], true);

  $('main-price').textContent = '—';
  $('main-change').textContent = '—';

  $('forecast-cards').innerHTML =
    '<div class="empty compact">Loading forecasts…</div>';

  $('chart-message').textContent = 'Loading candles…';
  $('chart-message').classList.remove('hidden');

  renderStats({items: []});

  $('history-body').innerHTML =
    '<tr><td colspan="6"><div class="empty">Loading…</div></td></tr>';

  updateSelectedSymbolUI();
  changeView();
}

document.querySelectorAll('[data-symbol]').forEach(button => {
  button.addEventListener('click', () => {
    if (button.dataset.symbol === state.symbol) return;
    selectSymbol(button.dataset.symbol);
  });
});

document.querySelectorAll('[data-interval]').forEach(button => {
  button.addEventListener('click', () => {
    state.interval = button.dataset.interval;

    document.querySelectorAll('[data-interval]').forEach(b => {
      b.classList.toggle('active', b === button);
      b.setAttribute('aria-pressed', b === button);
    });

    changeView();
  });
});

document.querySelectorAll('[data-horizon]').forEach(button => {
  button.addEventListener('click', () => {
    state.horizon = Number(button.dataset.horizon);
    state.offset = 0;

    document.querySelectorAll('[data-horizon]').forEach(b => {
      b.classList.toggle('active', b === button);
      b.setAttribute('aria-pressed', b === button);
    });

    changeView();
  });
});

$('previous').addEventListener('click', () => {
  state.offset = Math.max(0, state.offset - 20);
  changeView();
});

$('next').addEventListener('click', () => {
  state.offset += 20;
  changeView();
});

$('zoom-in').addEventListener('click', () => chart.zoom(.8));
$('zoom-out').addEventListener('click', () => chart.zoom(1.25));
$('chart-reset').addEventListener('click', () => chart.reset());

$('refresh').addEventListener('click', () => refreshAll());

document.querySelectorAll('.nav-link').forEach(link =>
  link.addEventListener('click', () => {
    document.querySelectorAll('.nav-link').forEach(a =>
      a.classList.toggle('active', a === link)
    );
  })
);

let quoteBusy = false;

setInterval(async () => {
  if (document.hidden || quoteBusy) return;

  quoteBusy = true;

  const version = state.version;
  const symbol = state.symbol;

  try {
    const quote = await api(
      '/api/quote?symbol=' + encodeURIComponent(symbol)
    );

    if (version === state.version) {
      updateQuote(quote);
    }
  } catch {
  } finally {
    quoteBusy = false;
  }
}, 15000);

setInterval(() => {
  if (!document.hidden) refreshAll();
}, 30000);

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshAll();
});

const symbolSearch = $('symbol-search');

symbolSearch?.addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;

  event.preventDefault();

  let symbol = symbolSearch.value
    .trim()
    .toUpperCase()
    .replace(/\s+/g, '')
    .replace(/\//g, '')
    .replace(/-/g, '');

  if (!symbol) return;

  if (!symbol.endsWith('USDT')) {
    symbol += 'USDT';
  }

  symbolSearch.value = symbol.slice(0, -4);

  selectSymbol(symbol);
});

updateSelectedSymbolUI();
refreshAll();
