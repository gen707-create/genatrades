# 📈 GenaTrades — Swing Trading Dashboard

**Автор:** Gennady  
**Стратегии:** Minervini SEPA · O'Neil CANSLIM · Mean Reversion  
**Горизонт:** 5 дней — 1 месяц  
**Последнее обновление:** Май 2026

---

## 🗂️ Структура проекта

```
Invest Stocks Portfolio/
├── tv_enrich.py          # Главный пайплайн (1800+ строк)
├── finviz_scan.py        # Сканер акций через Finviz Elite
├── swing-trader.skill    # Пакет навыков для Claude Cowork
├── .gitignore
└── README.md
```

---

## 🚀 Быстрый старт

### 1. Зависимости

```bash
pip install tradingview-screener yfinance requests pandas
```

### 2. Запуск — полный пайплайн

```bash
# Сканировать + обогатить + сгенерировать дашборд
python finviz_scan.py --strategy minervini | \
python tv_enrich.py --strategy minervini --html --output dashboard.html
```

### 3. Только конкретные тикеры

```bash
python tv_enrich.py --tickers NVDA,TSLA,AAPL --strategy minervini --html --output dashboard.html
```

### 4. Из файла

```bash
python tv_enrich.py --file tickers.txt --strategy canslim --html
```

---

## 📊 Дашборд — возможности

### Главная таблица (колонки)
| Колонка | Описание |
|---------|----------|
| Ticker | Тикер + точка сентимента новостей (🟢/🔴) |
| Sector | Сектор |
| Price | Текущая цена |
| Day% | Изменение за день |
| Pre / Post | Данные pre/post market |
| Earnings | Ближайшие отчёты |
| Entry | Точка входа |
| Stop | Стоп-лосс |
| T1 | Первый таргет |
| R/R | Соотношение риск/прибыль |
| Conv. | Уровень убеждённости (HIGH/MED/LOW) |
| ✓ | Количество пройденных критериев |
| Pattern | Паттерн (VCP / CUP+HANDLE / FLAT BASE) |
| Insider | Инсайдерские покупки/продажи |
| Inst. Top | Топ институциональный держатель |
| ★ | Добавить в watchlist |

### Детальная карточка (клик по строке)
- **RS LINE** — график относительной силы vs SPY + значения 1 ATR и 1.5 ATR
- **ANALYST CONSENSUS** — рейтинг аналитиков, целевая цена, + Perf WEEK / MONTH / QUARTER
- **LATEST NEWS** — последние 3 новости с сентиментом (🟢/🔴/⚪)
- **CHART PATTERN** — детали паттерна: недели в базе, глубина, волатильность, точка входа
- **INSIDER** — покупки/продажи инсайдеров за последние 6 месяцев
- **INSTITUTIONAL HOLDERS** — топ держатели, изменение позиций (▲/▼), дата последнего изменения

### Market Pulse (верхняя панель)
- SPY · QQQ · IWM (цена + день%)
- **Gold · Silver · Oil · Bitcoin**
- Секторные ETF (XLK, XLV, XLE, XLF, XLY)
- Режим рынка: Bull / Bear / Choppy

---

## 🔬 Стратегии

### Minervini SEPA
Критерии (core):
- Цена > SMA50 > SMA150 > SMA200
- SMA200 растёт ≥ 1 месяц
- Цена в пределах 25% от 52-нед. максимума
- RSI ≥ 70
- Относительная сила vs рынок

### O'Neil CANSLIM
Критерии:
- EPS рост > 25% квартал к кварталу
- Выручка растёт > 20%
- Акция рядом с хаем
- Институциональная поддержка

### Mean Reversion (отскок)
Критерии:
- RSI < 30 (перепроданность)
- Откат > 20% от 52-нед. хая
- Выше SMA200

---

## 📐 Детектирование паттернов (Phase 3)

### VCP (Volatility Contraction Pattern) — Minervini
- Окно: 26 недель
- Минимум 3 сжатия
- Каждое сжатие меньше предыдущего (монотонное уменьшение)
- Объём падает на каждом сжатии
- Score: 0–100

### Cup & Handle
- Окно: 65 недель
- Глубина чаши: 10–35%
- Восстановление ≥ 70% от глубины
- Ручка ≤ 15% от цены

### Flat Base
- Минимум 5 недель
- Диапазон ≤ 15%
- Счётчик недель, балл, точка входа

---

## 📡 Источники данных

| Источник | Данные |
|----------|--------|
| TradingView Screener | Цены, RSI, SMA, Perf 1W/1M/3M, сектор |
| Yahoo Finance (`yfinance`) | Pre/Post market, отчёты, ATR, новости |
| Yahoo RSS | Заголовки новостей + сентимент |
| Finviz Elite | Первичный скрининг кандидатов |

### Pre/Post Market — 3-tier fallback
1. `fast_info.pre_market_price / post_market_price`
2. `t.info["preMarketPrice"] / t.info["postMarketPrice"]`
3. `t.history(period="1d", interval="1m", prepost=True)`

---

## 📰 Сентимент новостей

Ключевые слова (~80 слов):

**Позитив (🟢):** beat, surge, upgrade, record, growth, profit, rally, breakthrough...  
**Негатив (🔴):** miss, fall, downgrade, loss, cut, recall, investigation, lawsuit...  
**Нейтрал (⚪):** всё остальное

Агрегированный сентимент по топ-3 новостям отображается в заголовке панели LATEST NEWS.

---

## 📏 ATR (Average True Range)

- 14-периодный недельный ATR
- True Range = max(H−L, |H−PrevClose|, |L−PrevClose|)
- Отображается в панели RS LINE:
  - **1 ATR** = $X.XX (синий)
  - **1.5 ATR** = $X.XX (фиолетовый)

---

## ⚙️ Watchlist

- Кнопка ★ в каждой строке
- Данные хранятся в `localStorage` браузера
- Фильтр "Watchlist only" в заголовке таблицы
- Sticky-колонка (всегда видна при прокрутке)

---

## 🔧 Архитектура `tv_enrich.py`

```
main()
 ├── finviz_scan.py (pipe) или --tickers / --file
 ├── fetch_tv_data()          # TradingView Screener API
 ├── enrich_tickers()
 │    ├── score_minervini()   # SEPA критерии
 │    ├── score_canslim()     # CANSLIM критерии
 │    ├── score_reversion()   # Mean Reversion критерии
 │    └── calculate_trade_setup()  # Entry / Stop / T1 / R/R
 ├── fetch_market_context()   # SPY, QQQ, Gold, BTC...
 ├── fetch_yahoo_data()
 │    ├── Pre/Post market (3-tier)
 │    ├── Earnings calendar
 │    ├── Insider transactions
 │    ├── Institutional holders
 │    ├── Analyst consensus
 │    ├── RS points (weekly close)
 │    ├── _calc_atr()          # 14-period weekly ATR
 │    ├── detect_chart_patterns()  # VCP / Cup / Flat Base
 │    └── _fetch_news_rss()    # News + sentiment
 └── build_html_dashboard()   # Self-contained HTML
```

---

## 🔄 История разработки (changelog)

### Phase 1 — Базовый пайплайн
- `finviz_scan.py` — скрининг Finviz Elite с auth токеном
- `tv_enrich.py` — обогащение данными TradingView
- Базовый HTML дашборд: таблица + score

### Phase 2 — Расширенный дашборд
- Pre/Post market данные (3-tier fallback)
- Детальные карточки (клик по строке)
- RS LINE SVG график
- Инсайдерские транзакции
- Институциональные держатели (% позиции)
- Watchlist (★ кнопка, localStorage)
- Market Pulse бар (SPY/QQQ/IWM + секторные ETF)
- Sector Heatmap

### Phase 2.1 — Фиксы и улучшения
- Watchlist кнопка: `position:sticky; right:0` (не исчезает при прокрутке)
- `overflow-x:auto` на контейнере таблицы (не `overflow:hidden`)
- Институциональные держатели: изменение позиции ▲/▼ + дата

### Phase 3 — Паттерны + аналитика
- **Автодетект паттернов:** VCP, Cup+Handle, Flat Base
- **Commodities в Market Pulse:** Gold, Silver, Oil, Bitcoin
- **Сентимент новостей:** 🟢/🔴/⚪ в заголовках и главной таблице
- **ATR в RS LINE панели:** 1 ATR и 1.5 ATR в $
- **Perf WEEK/MONTH/QUARTER** в панели ANALYST CONSENSUS

---

## 🐛 Известные проблемы / заметки

| Проблема | Решение |
|----------|---------|
| Edit tool обрезает файлы >72KB | Всегда использовать `open().read()` / `str.replace()` / `open().write()` |
| `%` в HTML строках внутри `%`-форматирования | Экранировать: `str.replace("%", "%%")` |
| Pre/Post данные = None вне расширенных часов | Нормально: 4–9:30am и 4–8pm ET |
| `yfinance` спам "possibly delisted" | `logging.getLogger("yfinance").setLevel(logging.CRITICAL)` |
| `TypeError: bad operand for unary +` | Двойной `+` в многострочном выражении — убрать лишний |

---

## 🔐 Безопасность

> ⚠️ **Никогда не коммитить** токен Finviz Elite в репозиторий.  
> Токен передаётся через переменную окружения или указывается только в разговоре с Claude.

```bash
export FINVIZ_TOKEN=ваш-токен
```

---

## 📌 TODO / следующие шаги

- [ ] Автоматический запуск по расписанию (cron / Task Scheduler)
- [ ] Email/Telegram алерты при появлении новых VCP
- [ ] Бэктест паттернов на исторических данных
- [ ] Интеграция с Interactive Brokers API для прямого исполнения
- [ ] Портфельный трекер с P&L

---

## 🛠️ Git — первый пуш

```bash
cd "D:\Claude Projects\INVEST Stocks\Invest Stocks Portfolio"
git init
git add tv_enrich.py finviz_scan.py swing-trader.skill diag.py .gitignore README.md
git commit -m "Initial commit: Minervini/CANSLIM trading dashboard pipeline"
git branch -M main
git remote add origin https://github.com/gen707-create/genatrades.git
git push -u origin main
```

### Обновление после изменений

```bash
git add tv_enrich.py
git commit -m "описание изменений"
git push
```
