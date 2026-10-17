# Krypta MAX — Research Protocol v1

## Регистрация и lineage

- LIVE и RESEARCH никогда не смешиваются: исследовательские результаты не становятся LIVE-историей.
- Любой серьёзный эксперимент регистрируется **до запуска**. Заранее фиксируются hypothesis, strategy/version, commit, config, dataset snapshot, symbols, periods, evaluator, benchmarks, endpoints, metrics и acceptance criteria.
- Каждый эксперимент принадлежит обязательному непустому `study_family_id`. Связанные эксперименты одной гипотезы/семейства сохраняют один family ID.
- Если кандидат создан под влиянием просмотренных результатов предыдущих экспериментов, их ID указываются в `influenced_by_experiment_ids`. Для первого эксперимента список может быть пустым; пустые ID, дубликаты и ссылка на себя запрещены.
- Новый `experiment_id` не делает просмотренный final/test-период снова untouched. Переименование семейства также не отменяет фактическое влияние данных.
- Любое изменение hypothesis, parameters, periods или primary endpoint после просмотра результата означает новый эксперимент; влияние предыдущих экспериментов сохраняется в lineage. Уже зарегистрированный manifest не редактируется, в том числе самим backtest после запуска.
- Все эксперименты, включая `FAILED` и `ABORTED`, сохраняются. Терминальный результат записывается через Registry с исходными `experiment_id` и `manifest_hash`; неудачи не удаляются ради отбора удачных результатов.

## Canonical instrument identity

`symbol` сам по себе не является полной instrument identity. Минимальный manifest-формат — `instrument_contexts`: список объектов с пятью непустыми строковыми полями `venue`, `market_type`, `base_asset`, `quote_asset`, `venue_symbol`. Identity определяется всей пятёркой, например `BINANCE / SPOT / ETH / USDT / ETHUSDT`, а не названием coin или одним symbol. Это декларация контекста эксперимента, не instrument catalog и не поддержка новых venues в runtime.

- Immutable manifest сохраняет все пять полей. В примерах venue, market_type и asset codes записаны явно в uppercase; `venue_symbol` сохраняет обозначение конкретного venue. Нельзя автоматически выводить base/quote из symbol, удалять разделители или считать aliases разных venues эквивалентными.
- Один и тот же symbol на другом venue означает другой instrument context. Binance Spot ETHUSDT и ETH/USDT другого venue не считаются автоматически одним dataset. Даже совпадение instrument context не заменяет `dataset_id`, `dataset_hash` и описание периода/источника данных.
- `instrument_contexts` перечисляет используемые контексты; существующее `symbols` сохраняется для совместимости и не заменяет полную identity. Дополнительное `roles` помечает назначение (`ANALYTICS`, `EXECUTION`), но не входит в пятёрку instrument identity. Execution instrument фиксируется отдельно в `execution_assumptions.execution_instrument`, объектом из тех же пяти полей; он должен совпадать с контекстом роли `EXECUTION` в списке. Это явное описание ролей без каталога, surrogate IDs или механизма разрешения ссылок.
- Analytics instrument и future execution instrument могут различаться. Если это понадобится, оба указываются явно вместе с ролью; совпадение coin не даёт права неявно использовать котировки/свечи одного контекста для исполнения в другом. В trading-примере BTC и ETH — analytics inputs, а execution — только ETH Spot.
- Commission/spread/slippage, delay, sizing и exit assumptions относятся к явно указанному execution instrument context, не к абстрактному ETH и не ко всем инструментам сразу.

Для **НОВЫХ серьёзных trading experiments** полный instrument context и разделение analytics/execution обязательны по исследовательскому процессу **до регистрации и запуска**. Для новых forecast experiments также следует фиксировать контексты источников данных, как в шаблоне.

Registry v1 сохраняет эти дополнительные поля без изменения schema_version, включает их в canonical bytes/hash, но не валидирует их содержание или взаимные ссылки. Обязательность по процессу не является новой проверкой validator. Старые manifests без этих полей остаются валидны; их не переписывают и не дополняют задним числом. Изменение identity зарегистрированного эксперимента требует нового manifest/experiment_id с корректной lineage.

## Разделение периодов

| Период | Назначение и последствия просмотра |
| --- | --- |
| Development | Разработка и анализ; после просмотра можно менять стратегию. |
| Validation | Проверка заранее выбранного кандидата. Просмотренный результат уже влияет на дальнейшую разработку; это влияние декларируется. |
| Final test | Открывается только для заранее выбранного кандидата. После просмотра больше не считается untouched для следующих версий той же study family. |

Новое подтверждение после использования final-периода требует действительно нового неиспользованного периода, предпочтительно будущих данных. История использования данных важнее нового ID.

Периоды упорядочены, не пересекаются и задаются как `[start, end)`. ISO-дата означает полночь UTC; timestamp содержит явный часовой пояс. Соседние периоды допустимы.

Warmup может использовать данные до начала evaluation segment по заранее объявленной политике. Outcome сделки/forecast не должен пересекать запрещённую границу периода. Для trading граница определяется фактическим временем жизни всей кампании, включая выход, а не только forecast horizon; политика исключения таких кампаний фиксируется заранее.

## Метрики и критерии

FORECAST и TRADING используют общий принцип предварительной регистрации, но разные основные метрики. Primary endpoint и правила принятия выбираются до просмотра результата; secondary endpoints не подменяют неудачный primary endpoint.

| FORECAST | TRADING |
| --- | --- |
| Directional accuracy | Net PnL после всех расходов |
| Coverage | Return на всём выделенном капитале, включая неиспользованный |
| Benchmark excess | Max drawdown |
| UP/DOWN/FLAT distribution исходов | Exposure / time in market |
| Uncertainty: заранее выбранный метод оценки | Turnover, commissions, execution assumptions и benchmark comparison |

Accuracy не равна profitability. `WAIT` не считается правильным прогнозом: он входит в знаменатель coverage, но исключён из знаменателя directional accuracy и из correct/incorrect. Распределение фактических UP/DOWN/FLAT и распределение решений UP/DOWN/WAIT сообщаются отдельно.

До запуска фиксируются horizon, flat threshold, cohort сравнения, правила missing data и метод uncertainty, учитывающий временную зависимость наблюдений. Сравниваемые стратегии и benchmarks используют одинаковые периоды, доступные на момент решения данные и согласованные условия оценки.

## Исполнение в trading research

До запуска дополнительно определяются execution instrument, commission model, spread model, slippage model, execution delay, position size и exit rules. Указываются полный выделенный капитал, расчёт full-capital return, drawdown и правила преобразования benchmark-сигналов в сделки с сопоставимыми расходами и ограничениями.

- Entry price не может быть ценой, существовавшей до фактического момента решения; учитываются задержка и доступность исполнения.
- Неизвестный порядок событий внутри OHLC-свечи нельзя моделировать оптимистично. Правило неоднозначного исполнения объявляется заранее: например, консервативный исход либо исключение с явным отчётом.
- Заранее определяются выходы и обращение с кампаниями, которые пересекают границу evaluation segment. Короткий forecast horizon не оправдывает перенос trading outcome через запрещённую границу.

## Соответствие текущему Experiment Registry

Registry v1 проверяет обязательные поля и периоды, сохраняет canonical manifest/hash, lineage и immutable терминальный результат. Он автоматически создаёт отдельный `registration.json` с `experiment_id`, `manifest_hash` и локальным UTC `registered_at`. Идентичная повторная регистрация сохраняет timestamp; изменённый manifest под тем же ID отклоняется.

Registry не проверяет существование referenced IDs, полноту заявленного влияния, реальное содержимое dataset/config/commit или факт предыдущего просмотра периода. Политики execution, warmup, uncertainty и acceptance criteria сохраняются как декларации; их смысл и соблюдение не проверяются Registry. Это обязанности исследовательского процесса, а не уже реализованная автоматизация. Иммутабельность API не защищает от ручного изменения файлов вне API.

Шаблоны: [FORECAST](research/templates/forecast_research.example.json) и [TRADING](research/templates/trading_research.example.json). Все ID, identities, hashes, критерии и даты в них фиктивны. Диапазон 2000–2003 демонстрационный, не утверждает наличие данных, untouched-статус или реальные будущие test dates. Перед реальным запуском заменяются все `EXAMPLE`-значения, выбираются обоснованные периоды/критерии и заполняются все предпосылки. Принятие шаблона validator не означает готовность эксперимента к запуску; trading engine или интеграция здесь не создаются.

## Что Research Protocol НЕ доказывает

- Локальное `registered_at` не является внешним доказательством времени.
- Хороший backtest не доказывает будущую прибыль.
- Downloadable historical candles не являются настоящей LIVE point-in-time историей.
- Большое количество сделок не означает такое же количество независимых наблюдений.
