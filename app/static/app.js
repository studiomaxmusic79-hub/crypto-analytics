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
  new Date(value).toLocaleTimeString('ru-RU', {
    hour: '2-digit',
    minute: '2-digit'
  });

const dateText = value =>
  new Date(value).toLocaleString('ru-RU', {
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
  if (horizon === 30) return '30 минут';
  if (horizon === 1) return '1 час';
  return '4 часа';
};

const chart = new CandleChart($('chart'), c => {
  $('ohlc').textContent =
    `${dateText(c.time)}   O ${number(c.open)}   H ${number(c.high)}   ` +
    `L ${number(c.low)}   C ${number(c.close)}` +
    `${c.close_time > Date.now() ? ' · формируется' : ''}`;
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
        : `Сервис временно недоступен (${response.status})`
    );
  }

  return response.json();
}

function updateMarketCard(prefix, ticker) {
  const price = $(prefix + '-price');
  const change = $(prefix + '-change');

  if (!price || !change) return;

  price.textContent = '$' + number(ticker.price);
  change.textContent = signed(ticker.change_pct);
  change.className =
    'market-change ' +
    (ticker.change_pct >= 0 ? 'positive' : 'negative');
}

function updateQuote(ticker) {
  /*
    Старые карточки BTC/ETH больше не требуют
    отдельных запросов к Binance.

    Если элементы ещё присутствуют в HTML,
    они могут обновиться данными выбранной валюты
    только когда символ совпадает.
  */
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
      ticker.change_pct >= 0
        ? 'positive'
        : 'negative';

    $('updated').textContent =
      'Цена · ' + timeText(ticker.updated_at);
  }
}

function updateSelectedSymbolUI() {
  $('chart-title').innerHTML =
    `${displaySymbol(state.symbol)} ` +
    `<span class="exchange-label">BINANCE</span>`;

  $('export').href =
    '/api/export.csv?symbol=' +
    encodeURIComponent(state.symbol);

  document.querySelectorAll('[data-symbol]').forEach(button => {
    const selected =
      button.dataset.symbol === state.symbol;

    button.classList.toggle(
      'selected',
      selected
    );

    button.setAttribute(
      'aria-pressed',
      selected
    );
  });
}

function renderError() {
  const messages = [
    ...new Set(
      Object.values(state.errors).filter(Boolean)
    )
  ];

  $('error').textContent =
    messages.join(' ');

  $('error').classList.toggle(
    'hidden',
    !messages.length
  );

  $('connection-dot').className =
    'status-dot ' +
    (state.marketGood ? 'online' : 'offline');

  $('connection-text').textContent =
    state.marketGood
      ? (
          messages.length
            ? 'Данные Binance · есть замечания'
            : 'Binance · публичные данные'
        )
      : 'Данные недоступны или загружаются';
}

function renderForecasts(data) {
  if (!data.items.length) {
    $('forecast-cards').innerHTML =
      '<div class="empty compact">' +
      'Ожидаем первый расчёт.<br>' +
      'При недоступности данных прогноз не создаётся.' +
      '</div>';

    return;
  }

  $('forecast-cards').innerHTML =
    data.items.map(p => {
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
          ? 'Рост'
          : p.direction === 'DOWN'
            ? 'Снижение'
            : 'Наблюдаем';

      const stale =
        Date.now() - p.issued_at >
        17 * 60000;

      const status =
        Date.now() >= p.due_at
          ? 'Архивный · горизонт завершён'
          : stale
            ? 'Архивный · нет нового расчёта'
            : `Проверка в ${timeText(p.due_at)}`;

      const segments = Array.from(
        {length: 10},
        (_, i) =>
          `<i class="${
            (i + 1) * 10 <= p.confidence
              ? 'filled'
              : ''
          }"></i>`
      ).join('');

      return `
        <article class="forecast-card">
          <div class="forecast-top">
            <span class="horizon-label">
              На ${horizonText(p.horizon)}
            </span>

            <span class="forecast-target">
              ${status}
            </span>
          </div>

          <div class="forecast-direction ${kind}">
            <span class="direction-icon">
              ${icon}
            </span>

            <strong class="direction-label">
              ${p.direction}
            </strong>

            <span class="direction-detail">
              ${label}
            </span>
          </div>

          <div class="signal-strength">
            <span>Сила сигнала</span>
            <strong>
              ${p.confidence} / 100
            </strong>
          </div>

          <div class="strength-track ${p.direction.toLowerCase()}">
            ${segments}
          </div>

          <p class="reasons">
            ${escapeHTML(p.reasons[0])}
            · RSI ${p.indicators.rsi14.toFixed(0)}
            <br>
            Нейтральная зона
            ±${p.neutral_pct.toFixed(2)}%
            <br>
            Расчёт ${timeText(p.issued_at)}
            · база ${timeText(p.reference_at)}
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
    const s =
      data.items.find(
        x => x.horizon === h
      );

    $('accuracy-' + h).textContent =
      s?.directional_accuracy == null
        ? '—'
        : s.directional_accuracy.toFixed(1) + '%';

    $('sample-' + h).textContent =
      s?.directional_total
        ? `${s.directional_correct} из ${s.directional_total} направленных прогнозов`
        : 'Нет завершённых UP / DOWN';

    if (s) {
      total += s.total;
      evaluated += s.evaluated;
      waits += s.wait_total;
      waitCorrect += s.wait_correct;
    }
  }

  $('wait-score').textContent =
    waits
      ? (
          100 * waitCorrect / waits
        ).toFixed(1) + '%'
      : '—';

  $('wait-sample').textContent =
    waits
      ? `${waitCorrect} из ${waits} — внутри нейтральной зоны`
      : 'Нет завершённых WAIT';

  $('evaluated-total').textContent =
    evaluated;

  $('pending-total').textContent =
    total
      ? `${total - evaluated} ожидают проверки · ${total} создано`
      : 'История начнётся с первого расчёта';
}

function renderHistory(data) {
  $('history-body').innerHTML =
    data.items.length
      ? data.items.map(p => {
          const done =
            p.evaluated_at != null;

          const status =
            done
              ? (
                  p.correct
                    ? 'Подтверждён'
                    : 'Не подтверждён'
                )
              : (
                  p.due_at < Date.now()
                    ? 'Ожидает данных'
                    : 'Ожидает проверки'
                );

          const note =
            done
              ? `Проверен ${dateText(p.evaluated_at)}`
              : (
                  p.evaluation_note ||
                  `Целевая цена на ${dateText(p.due_at)}`
                );

          return `
            <tr>
              <td>
                ${dateText(p.issued_at)}
                <small>
                  База ${timeText(p.reference_at)}
                  · ${escapeHTML(p.model_version)}
                </small>
              </td>

              <td>
                ${horizonText(p.horizon)}
                <small>
                  До ${dateText(p.due_at)}
                </small>
              </td>

              <td>
                <span class="badge ${p.direction}">
                  ${
                    p.direction === 'UP'
                      ? '↗'
                      : p.direction === 'DOWN'
                        ? '↘'
                        : '→'
                  }
                  ${p.direction}
                </span>

                <small>
                  Сила ${p.confidence}/100
                </small>
              </td>

              <td>
                $${number(p.base_price)}
                <small>
                  Порог ±${p.neutral_pct.toFixed(2)}%
                </small>
              </td>

              <td>
                ${
                  done
                    ? `$${number(p.actual_price)}
                       <small class="${
                         p.change_pct >= 0
                           ? 'positive'
                           : 'negative'
                       }">
                         ${signed(p.change_pct)}
                         · ${p.observed_direction}
                       </small>`
                    : '—'
                }
              </td>

              <td title="${escapeHTML(note)}">
                <span class="result-dot ${
                  done
                    ? (
                        p.correct
                          ? 'win'
                          : 'loss'
                      )
                    : ''
                }"></span>

                ${status}

                <small>
                  ${
                    done
                      ? 'Фактическое движение'
                      : 'Проверяем по закрытию 1м'
                  }
                </small>
              </td>
            </tr>
          `;
        }).join('')
      : `
        <tr>
          <td colspan="6">
            <div class="empty">
              Здесь появятся реальные прогнозы
              и результаты их проверки.
              <br>
              Первый результат — после завершения
              горизонта 30 минут.
            </div>
          </td>
        </tr>
      `;

  $('history-count').textContent =
    data.total
      ? `${state.offset + 1}–${
          Math.min(
            state.offset + 20,
            data.total
          )
        } из ${data.total} записей`
      : '0 записей';

  $('previous').disabled =
    state.offset === 0;

  $('next').disabled =
    state.offset + 20 >= data.total;
}

function renderStatus(data) {
  state.errors.worker =
    Object.values(data.errors).join(' ');

  const notice =
    data.storage === 'sqlite'
      ? (
          'Локальный запуск: история хранится ' +
          'в файле data/crypto.db на этом компьютере. ' +
          'Для Render настроен PostgreSQL.'
        )
      : data.render_free
        ? (
            'На бесплатном Render сервер может засыпать. ' +
            'После пробуждения результаты сохранённых ' +
            'прогнозов будут проверены; пропущенные ' +
            'прогнозы не добавляются задним числом.'
          )
        : '';

  $('storage-notice').textContent =
    notice;

  $('storage-notice').classList.toggle(
    'hidden',
    !notice
  );
}

async function refreshAll() {
  const version = state.version;
  const symbol = state.symbol;

  if (state.busy === version + 1) {
    return;
  }

  state.busy =
    version + 1;

  $('refresh-icon').classList.add(
    'spin'
  );

  const params =
    `symbol=${encodeURIComponent(symbol)}`;

  const chartKey =
    symbol + state.interval;

  /*
    ВАЖНО:
    Раньше здесь были ещё два отдельных запроса:
    BTCUSDT и ETHUSDT.

    Они удалены.

    Теперь Binance получает рыночный запрос
    только для выбранной пользователем валюты.
  */
  const tasks = [
    [
      'market',
      `/api/market?${params}&interval=${state.interval}`,
      data => {
        updateQuote(
          data.ticker
        );

        chart.setData(
          data.candles,
          state.lastChartKey !== chartKey
        );

        state.lastChartKey =
          chartKey;

        state.marketGood =
          true;

        $('chart-message').classList.add(
          'hidden'
        );
      }
    ],

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

  const results =
    await Promise.allSettled(
      tasks.map(
        task => api(task[1])
      )
    );

  if (state.version !== version) {
    state.busy = false;

    $('refresh-icon').classList.remove(
      'spin'
    );

    return;
  }

  results.forEach(
    (result, index) => {
      const [key, , render] =
        tasks[index];

      if (result.status === 'fulfilled') {
        delete state.errors[key];

        render(
          result.value
        );
      } else {
        state.errors[key] =
          result.reason.name === 'TimeoutError'
            ? (
                'Сервер отвечает медленно. ' +
                'Повторим запрос автоматически.'
              )
            : result.reason.message;

        if (key === 'market') {
          state.marketGood =
            false;

          $('chart-message').textContent =
            'Не удалось обновить график. ' +
            'Последние полученные свечи ' +
            'могут быть устаревшими.';

          $('chart-message').classList.remove(
            'hidden'
          );
        }

        if (key === 'history') {
          $('history-body').innerHTML =
            '<tr><td colspan="6">' +
            '<div class="empty">' +
            'Журнал временно недоступен. ' +
            'Повторим запрос автоматически.' +
            '</div></td></tr>';
        }

        if (key === 'forecasts') {
          $('forecast-cards').innerHTML =
            '<div class="empty compact">' +
            'Прогнозы временно недоступны.<br>' +
            'Повторим запрос автоматически.' +
            '</div>';
        }
      }
    }
  );

  renderError();

  state.busy =
    false;

  $('refresh-icon').classList.remove(
    'spin'
  );
}

function changeView() {
  state.version++;
  state.busy = false;
  refreshAll();
}

function selectSymbol(symbol) {
  state.symbol =
    symbol;

  state.offset =
    0;

  state.marketGood =
    false;

  state.errors =
    {};

  chart.setData(
    [],
    true
  );

  $('main-price').textContent =
    '—';

  $('main-change').textContent =
    '—';

  $('forecast-cards').innerHTML =
    '<div class="empty compact">' +
    'Загружаем прогнозы…' +
    '</div>';

  $('chart-message').textContent =
    'Загружаем свечи…';

  $('chart-message').classList.remove(
    'hidden'
  );

  renderStats({
    items: []
  });

  $('history-body').innerHTML =
    '<tr><td colspan="6">' +
    '<div class="empty">' +
    'Загрузка…' +
    '</div></td></tr>';

  updateSelectedSymbolUI();
  changeView();
}

document
  .querySelectorAll('[data-symbol]')
  .forEach(button => {
    button.addEventListener(
      'click',
      () => {
        if (
          button.dataset.symbol ===
          state.symbol
        ) {
          return;
        }

        selectSymbol(
          button.dataset.symbol
        );
      }
    );
  });

document
  .querySelectorAll('[data-interval]')
  .forEach(button => {
    button.addEventListener(
      'click',
      () => {
        state.interval =
          button.dataset.interval;

        document
          .querySelectorAll('[data-interval]')
          .forEach(b => {
            b.classList.toggle(
              'active',
              b === button
            );

            b.setAttribute(
              'aria-pressed',
              b === button
            );
          });

        changeView();
      }
    );
  });

document
  .querySelectorAll('[data-horizon]')
  .forEach(button => {
    button.addEventListener(
      'click',
      () => {
        state.horizon =
          Number(
            button.dataset.horizon
          );

        state.offset =
          0;

        document
          .querySelectorAll('[data-horizon]')
          .forEach(b => {
            b.classList.toggle(
              'active',
              b === button
            );

            b.setAttribute(
              'aria-pressed',
              b === button
            );
          });

        changeView();
      }
    );
  });

$('previous').addEventListener(
  'click',
  () => {
    state.offset =
      Math.max(
        0,
        state.offset - 20
      );

    changeView();
  }
);

$('next').addEventListener(
  'click',
  () => {
    state.offset += 20;
    changeView();
  }
);

$('zoom-in').addEventListener(
  'click',
  () => chart.zoom(.8)
);

$('zoom-out').addEventListener(
  'click',
  () => chart.zoom(1.25)
);

$('chart-reset').addEventListener(
  'click',
  () => chart.reset()
);

$('refresh').addEventListener(
  'click',
  () => refreshAll()
);

document
  .querySelectorAll('.nav-link')
  .forEach(link =>
    link.addEventListener(
      'click',
      () => {
        document
          .querySelectorAll('.nav-link')
          .forEach(a =>
            a.classList.toggle(
              'active',
              a === link
            )
          );
      }
    )
  );


/*
  Быстрое обновление только текущей цены.

  Раньше: каждые 15 секунд.
  Теперь: каждые 30 секунд.

  Запрашивается только выбранная валюта.
*/
let quoteBusy = false;

setInterval(
  async () => {
    if (
      document.hidden ||
      quoteBusy
    ) {
      return;
    }

    quoteBusy =
      true;

    const version =
      state.version;

    const symbol =
      state.symbol;

    try {
      const quote =
        await api(
          '/api/quote?symbol=' +
          encodeURIComponent(symbol)
        );

      if (
        version === state.version
      ) {
        updateQuote(
          quote
        );
      }
    } catch {
      // Основной refreshAll покажет
      // ошибку соединения при необходимости.
    } finally {
      quoteBusy =
        false;
    }
  },
  30000
);


/*
  Полное обновление:

  график,
  прогнозы,
  история,
  статистика,
  состояние сервера.

  Раньше: каждые 30 секунд.
  Теперь: каждые 2 минуты.
*/
setInterval(
  () => {
    if (!document.hidden) {
      refreshAll();
    }
  },
  120000
);


/*
  Если пользователь вернулся
  на вкладку сайта — обновляем данные сразу.
*/
document.addEventListener(
  'visibilitychange',
  () => {
    if (!document.hidden) {
      refreshAll();
    }
  }
);


const symbolSearch =
  $('symbol-search');

symbolSearch?.addEventListener(
  'keydown',
  event => {
    if (event.key !== 'Enter') {
      return;
    }

    event.preventDefault();

    let symbol =
      symbolSearch.value
        .trim()
        .toUpperCase()
        .replace(/\s+/g, '')
        .replace(/\//g, '')
        .replace(/-/g, '');

    if (!symbol) {
      return;
    }

    if (!symbol.endsWith('USDT')) {
      symbol += 'USDT';
    }

    symbolSearch.value =
      symbol.slice(0, -4);

    selectSymbol(
      symbol
    );
  }
);


updateSelectedSymbolUI();
refreshAll();
