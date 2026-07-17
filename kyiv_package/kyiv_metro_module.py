"""
kyiv_metro_module.py
=====================
Метро + фунікулер для CityTwin, у ТІЙ САМІЙ структурі GtfsFeed, що й
трамвай/тролейбус/автобус у kyiv_backend.py — щоб уся інша машинерія
(Дейкстра, /routes, легенда, expand_frequencies) працювала однаково,
без спецвипадків для метро.

ЧОМУ ЦЕЙ ФАЙЛ ІСНУЄ ОКРЕМО ВІД load_rail_like_feed() З kyiv_backend.py
-----------------------------------------------------------------------
У kyiv_backend.py вже є універсальний завантажувач load_rail_like_feed(),
і в CONFIG навіть прописано шлях під нього:
    UNDERGROUND_STOPS_PATH = "stopsInterchangeUnderground.csv"

Це ПОМИЛКА, яку варто виправити (і причина, чому метро зараз не
з'являється в бекенді): load_rail_like_feed() як "stops" очікує файл
з колонками stop_id/stop_name/lat/lon — координати станцій. Але
stopsInterchangeUnderground.json — це ЗОВСІМ ІНША таблиця: пари
"станція метро <-> зупинка наземного транспорту" для пішохідних
пересадок (from_name/to_name/comment), БЕЗ жодних координат. Тому
_sniff_field() там ніколи не знайде lat/lon, і load_rail_like_feed()
завжди повертатиме None для метро — тихо, без падіння програми, тому
це легко не помітити.

Офіційний ресурс зі списком станцій метро + координатами (аналог
"stops.txt" GTFS) називається на порталі, судячи з усього, "underground"
або "stopsUnderground" — окремий ресурс від "stopsInterchangeUnderground".
Я НЕ маю доступу до мережі в цій пісочниці, тому не можу перевірити
точну назву/URL ресурсу самостійно. Файл із ним не був завантажений у
цю сесію.

ЩО Є В ДАНИХ, ЯКІ ТИ ЗАВАНТАЖИВ, І ЩО Я З НИХ РОБЛЮ:
  - timePeriodUnderground.json / timePeriodKyivFunicular.json:
        РЕАЛЬНІ інтервали руху (headway) по лініях, погодинно,
        окремо будні/вихідні, окремо прямий/зворотній напрямок.
        Це саме те, чого бракує фронтендовій JS-симуляції (там
        waitFor() використовує грубі "на око" числа 1.75/4.5/5.5 хв).
        Я парсю ці файли й перетворюю на frequencies.txt-подібні
        записи — сумісні з expand_frequencies() у kyiv_backend.py.
  - stopsInterchangeUnderground.json:
        Реальні пари пересадок метро<->наземний транспорт (333 шт).
        Координат немає, тому напряму в граф як ребра їх не вставити
        (не знаємо, де саме на карті ця "from_name"/"to_name" зупинка
        наземного транспорту). Зберігаю як довідкову таблицю
        (feed.transfer_hints) — якщо колись підвантажиш GTFS
        Київпастрансу з тими ж stop_name, можна буде зіставити по
        назві й додати явні ребра пересадки замість геометричного
        "найближчий вузол у радіусі 400м".

ГЕОМЕТРІЯ СТАНЦІЙ МЕТРО (STATION LAT/LNG) — ЗВІДКИ ВОНА ТУТ:
  У жодному із завантажених для метро JSON (timePeriodUnderground.json,
  stopsInterchangeUnderground.json, stopTimesUnderground.json) НЕМАЄ
  координат станцій — жоден із них не є "списком станцій", усі три це
  таблиці розкладу/пересадок з посиланням на станцію лише за назвою.
  Тому для метро я й далі беру ті самі точки, які вже використовує
  kyiv-chas.html (const METRO_LINES) для фронтендової JS-симуляції —
  вони там і так намальовані як "приблизна" схема (не офіційна геометрія
  тунелів). Позначено "approx": True (як і в build_routes_geojson() для
  трамваїв без shapes.txt) — чесно, а не видається за офіційні дані.
  ЯК ЩЕ ПОКРАЩИТИ: якщо колись знайдеться офіційний ресурс "underground"/
  "stopsUnderground", НЕ потрібно чіпати METRO_LINES/build_metro_feed —
  досить покласти поруч файл `stopsUnderground_override.geojson` у ТОМУ Ж
  форматі, що й `stopsUnderground_approx.geojson` нижче (FeatureCollection,
  Point [lng,lat], properties.code1 == "metro:{lineKey}:{назва}" або
  "funicular:{назва}"). build_metro_feed() сам підхопить його координати
  замість approx (та сама механіка override-файла, що описана нижче).

  ФОРМАЛІЗОВАНО В ОКРЕМИЙ РЕСУРС (stopsUnderground_approx.geojson):
  ті самі approx-точки з METRO_LINES/FUNICULAR_PTS нижче тепер ще й
  збережені як geojson-файл (той самий формат, що stopsKyivCityExpress.geojson
  — це і є заготовка під майбутню заміну реальними даними без правок коду.
  Хардкод у METRO_LINES/FUNICULAR_PTS лишається fallback-джерелом (модуль
  працює і без цього файлу), а файл — необов'язковий override "якщо
  з'явиться щось точніше, поклади сюди". load_metro_coords_override()
  читає код1->координати з нього, якщо файл існує; будь-який код1 з
  файлу перекриває відповідну точку з METRO_LINES/FUNICULAR_PTS.

ГЕОМЕТРІЯ СТАНЦІЙ ЕЛЕКТРИЧКИ (KyivCityExpress) — ЦЕ ВЖЕ ВИРІШЕНО:
  На відміну від метро, для електрички завантажено ПОВНИЙ комплект
  реальних даних:
    - stopsKyivCityExpress.geojson — 34 РЕАЛЬНІ точки (Point geometry)
      з полем properties.code1, що напряму (без fuzzy-матчингу за
      назвою) збігається з полем code1 у розкладі нижче.
    - stopTimesKyivCityExpress.json — РЕАЛЬНИЙ поїзний розклад: 50
      потягів (train), для кожного — впорядкована (за objectid)
      послідовність (code1, arrival, departure, num_route, type).
      Це вже готовий stop_times.txt по суті (конкретні часи конкретних
      потягів), а НЕ погодинний headway, як у метро.
  Тому build_rail_feed() нижче не наближує нічого і не потребує
  expand_frequencies(): кожен 'train' стає одним реальним trip_id з
  точними stop_times. Поле 'type' ('щоденно' / 'крім сб., нд.') —
  де-факто calendar.txt для електрички, єдине місце в проєкті, де день
  тижня фільтрує РЕАЛЬНІ рейси, а не наближений headway.
"""

import json
import os
from typing import Optional

import kyiv_backend as base  # переюзаємо GtfsFeed, haversine_m


# Необов'язковий файл-підміна координат метро/фунікулера (див. докстрінг
# вище). Якщо його немає поруч — тихо ігнорується, як і решта опційних
# ресурсів у проєкті; METRO_LINES/FUNICULAR_PTS нижче лишаються fallback.
METRO_COORDS_OVERRIDE_PATH = "stopsUnderground_override.geojson"


def load_metro_coords_override(path: str = METRO_COORDS_OVERRIDE_PATH) -> dict:
    """code1 -> (lat, lng, name) з необов'язкового override-файла. Формат —
    той самий geojson, що stopsKyivCityExpress.geojson / stopsUnderground_approx.geojson.
    Повертає {} мовчки, якщо файла немає (це не помилка — override не обов'язковий)."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    out = {}
    for feat in data.get("features", []):
        props = feat["properties"]
        lng, lat = feat["geometry"]["coordinates"][0], feat["geometry"]["coordinates"][1]
        out[props["code1"]] = (lat, lng, props.get("name", ""))
    return out


# ---------------------------------------------------------------------------
# Геометрія ліній метро й фунікулера (джерело — kyiv-chas.html, const
# METRO_LINES / FUNICULAR; ті самі точки, що вже бачить користувач на
# карті у швидкій JS-симуляції). Порядок станцій у списку — це порядок
# проходження лінії від першого кінцевого до другого.
# ---------------------------------------------------------------------------
METRO_LINES = {
    "red": {  # Святошинсько-Броварська
        "official_name": "Святошинсько-Броварська",
        "color": "#E8434F",
        "names": ['Академмістечко', 'Житомирська', 'Святошин', 'Нивки', 'Берестейська', 'Шулявська',
                  'Політехнічний інститут', 'Вокзальна', 'Університет', 'Театральна', 'Хрещатик', 'Арсенальна',
                  'Дніпро', 'Гідропарк', 'Лівобережна', 'Дарниця', 'Чернігівська', 'Лісова'],
        "pts": [
            [50.4877, 30.3721], [50.4820, 30.3796], [50.4757, 30.3846], [50.4670, 30.3987],
            [50.4611, 30.4136], [50.4534, 30.4310], [50.4499, 30.4508], [50.4460, 30.4737],
            [50.4444, 30.5106], [50.4469, 30.5192], [50.4478, 30.5238], [50.4472, 30.5423],
            [50.4411, 30.5586], [50.4456, 30.5769], [50.4514, 30.5989], [50.4470, 30.6165],
            [50.4467, 30.6250], [50.4436, 30.6396],
        ],
    },
    "blue": {  # Оболонсько-Теремківська
        "official_name": "Оболонсько-Теремківська",
        "color": "#2E86DE",
        "names": ['Героїв Дніпра', 'Мінська', 'Оболонь', 'Почайна', 'Тараса Шевченка', 'Контрактова площа',
                  'Поштова площа', 'Майдан Незалежності', 'Площа Українських Героїв', 'Олімпійська',
                  'Палац «Україна»', 'Либідська', 'Деміївська', 'Голосіївська', 'Васильківська',
                  'Виставковий центр', 'Іподром', 'Теремки'],
        "pts": [
            [50.5388, 30.4956], [50.5225, 30.4970], [50.5054, 30.4979], [50.4889, 30.5040],
            [50.4750, 30.5110], [50.4661, 30.5188], [50.4580, 30.5210], [50.4501, 30.5237],
            [50.4372, 30.5195], [50.4270, 30.5182], [50.4185, 30.5230], [50.4132, 30.5236],
            [50.4000, 30.5157], [50.3897, 30.5040], [50.3820, 30.4970], [50.3800, 30.4900],
            [50.3790, 30.4820], [50.3765, 30.4750],
        ],
    },
    "green": {  # Сирецько-Печерська
        "official_name": "Сирецько-Печерська",
        "color": "#33A65C",
        "names": ['Сирець', 'Дорогожичі', 'Лук\u2019янівська', 'Золоті ворота', 'Палац спорту', 'Кловська',
                  'Печерська', 'Звіринецька', 'Видубичі', 'Славутич', 'Осокорки', 'Позняки', 'Харківська',
                  'Вирлиця', 'Бориспільська', 'Червоний хутір'],
        "pts": [
            [50.4762, 30.4361], [50.4700, 30.4600], [50.4614, 30.4885], [50.4472, 30.5152],
            [50.4362, 30.5215], [50.4300, 30.5320], [50.4270, 30.5420], [50.4200, 30.5500],
            [50.4130, 30.5600], [50.4020, 30.5720], [50.3980, 30.5850], [50.4050, 30.6050],
            [50.4080, 30.6200], [50.4020, 30.6350], [50.3950, 30.6550], [50.3813, 30.6889],
        ],
    },
}

FUNICULAR_NAMES = ["Поділ", "Михайлівська"]
FUNICULAR_PTS = [[50.4589, 30.5257], [50.4635, 30.5268]]

# line у timePeriodUnderground.json -> ключ у METRO_LINES
LINE_NAME_TO_KEY = {
    "Святошинсько-Броварська": "red",
    "Оболонсько-Теремківська": "blue",
    "Сирецько-Печерська": "green",
}

METRO_SPEED_KMH = 33.0
METRO_DWELL_S = 25          # орієнтовна стоянка на проміжній станції
FUNICULAR_TRIP_S = 3 * 60   # орієнтовна тривалість рейсу фунікулера (~3 хв)


# ---------------------------------------------------------------------------
# Парсинг хв:сек / год:хв
# ---------------------------------------------------------------------------
def _mmss_to_s(tok: str) -> int:
    m, s = tok.strip().split(":")
    return int(m) * 60 + int(s)


def _mmss_maybe_range_to_s(val: str) -> float:
    """'15:00' -> 900. '7:30-5:30' (інтервал змінюється в межах періоду —
    на початку періоду один headway, в кінці інший) -> середнє двох меж.
    Це спрощення: реальний інтервал усередині періоду точно не лінійний,
    але для ізохрони точності "середній інтервал за годину" достатньо."""
    val = val.strip()
    if "-" in val:
        a, b = val.split("-", 1)
        return (_mmss_to_s(a) + _mmss_to_s(b)) / 2
    return _mmss_to_s(val)


def _hhmm_to_s(tok: str) -> int:
    h, m = tok.strip().split(":")
    return int(h) * 3600 + int(m) * 60


def _hhmm_range_to_s(val: str):
    a, b = val.split("-", 1)
    return _hhmm_to_s(a), _hhmm_to_s(b)


# ---------------------------------------------------------------------------
# Завантаження JSON-ресурсів (формат ArcGIS FeatureServer: features[].attributes)
# ---------------------------------------------------------------------------
def _load_features(path: str) -> list:
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    return [feat["attributes"] for feat in data.get("features", [])]


def load_underground_time_periods(path: str) -> dict:
    """line -> [(start_s, end_s, headway_st_weekday_s, headway_rv_weekday_s,
                 headway_st_holiday_s, headway_rv_holiday_s)], відсортовано за часом."""
    out: dict = {}
    for row in _load_features(path):
        line = row["line"]
        start_s, end_s = _hhmm_range_to_s(row["timeperiod"])
        entry = (
            start_s, end_s,
            _mmss_maybe_range_to_s(row["st_weekday"]),
            _mmss_maybe_range_to_s(row["rv_weekday"]),
            _mmss_maybe_range_to_s(row["st_holiday"]),
            _mmss_maybe_range_to_s(row["rv_holiday"]),
        )
        out.setdefault(line, []).append(entry)
    for line in out:
        out[line].sort(key=lambda e: e[0])
    return out


def load_funicular_time_periods(path: str) -> list:
    """[(start_s, end_s, up_weekday_s, down_weekday_s, up_holiday_s, down_holiday_s)]"""
    out = []
    for row in _load_features(path):
        start_s, end_s = _hhmm_range_to_s(row["timeperiod"])
        out.append((
            start_s, end_s,
            _mmss_maybe_range_to_s(row["st_weekday"]),
            _mmss_maybe_range_to_s(row["rv_weekday"]),
            _mmss_maybe_range_to_s(row["st_holiday"]),
            _mmss_maybe_range_to_s(row["rv_holiday"]),
        ))
    out.sort(key=lambda e: e[0])
    return out


def load_interchange_transfers(path: str) -> list:
    """Довідкова таблиця пересадок метро<->наземний транспорт (без координат —
    див. докстрінг модуля). [(from_name, to_name, comment)]"""
    out = []
    for row in _load_features(path):
        out.append((row.get("from_name", ""), row.get("to_name", ""), row.get("comment", "")))
    return out


# ---------------------------------------------------------------------------
# Київська міська електричка (KyivCityExpress) — РЕАЛЬНІ дані (не approx)
# ---------------------------------------------------------------------------
def _hhmm_to_s_or_none(tok: Optional[str]) -> Optional[int]:
    if not tok:
        return None
    h, m = tok.strip().split(":")
    return int(h) * 3600 + int(m) * 60


def load_rail_stops(path: str) -> dict:
    """code1 -> (lat, lng, name) із stopsKyivCityExpress.geojson.

    Це звичайний GeoJSON FeatureCollection (geometry.coordinates = [lng,lat]),
    НЕ ArcGIS attributes-обгортка на кшталт інших файлів модуля — тому
    парситься окремо від _load_features(), а не через неї."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    out = {}
    for feat in data.get("features", []):
        props = feat["properties"]
        lng, lat = feat["geometry"]["coordinates"][0], feat["geometry"]["coordinates"][1]
        out[props["code1"]] = (lat, lng, props.get("name", ""))
    return out


def load_rail_stop_times(path: str) -> dict:
    """train -> [рядки], відсортовано за objectid (= реальний порядок
    проходження станцій цим конкретним потягом — перевірено на даних:
    50 потягів, для жодного objectid-порядок не суперечить зростанню часу,
    тип обслуговування (type) і маршрут (num_route) не змінюються в межах
    одного train)."""
    trains: dict = {}
    for row in _load_features(path):
        trains.setdefault(row["train"], []).append(row)
    for tid in trains:
        trains[tid].sort(key=lambda r: r["objectid"])
    return trains


def build_rail_feed(stops_path: str, stoptimes_path: str, day_type: str = "weekday") -> "base.GtfsFeed":
    """
    Будує GtfsFeed для міської електрички з РЕАЛЬНОГО поїзного розкладу.

    На відміну від build_metro_feed() (де є лише погодинний headway і
    доводиться синтетично розгортати frequencies), тут для кожного потяга
    вже є точні (arrival, departure) по кожній станції — тому rail НЕ
    проходить через expand_frequencies() і не потребує його.

    day_type="weekday" включає потяги обох типів обслуговування
    ('щоденно' і 'крім сб., нд.'); будь-яке інше значення (напр.
    "weekend"/"holiday") включає лише 'щоденно' — оскільки 'type' тут
    реальний прапорець курсування, а не приблизна оцінка.
    """
    stops = load_rail_stops(stops_path)
    trains = load_rail_stop_times(stoptimes_path)

    feed = base.GtfsFeed()
    for code, (lat, lng, name) in stops.items():
        feed.stops[f"rail:{code}"] = (lat, lng, name)

    n_included, n_skipped_daytype, n_skipped_short = 0, 0, 0
    for train_id, rows in trains.items():
        service_type = rows[0]["type"]
        if service_type != "щоденно" and day_type != "weekday":
            n_skipped_daytype += 1
            continue

        rows = [r for r in rows if r["code1"] in stops]
        if len(rows) < 2:
            n_skipped_short += 1
            continue

        route_name = rows[0]["num_route"]
        route_id = f"rail:{route_name}"
        if route_id not in feed.routes:
            feed.routes[route_id] = (route_name, "2")  # GTFS route_type 2 = rail

        stop_times = []
        for i, r in enumerate(rows):
            dep = _hhmm_to_s_or_none(r["departure"])
            arr = _hhmm_to_s_or_none(r["arrival"])
            # перша станція рейсу має лише departure, остання — лише arrival;
            # feed.stop_times очікує обидва поля, тож дублюємо наявне значення
            # в порожнє (типова практика GTFS-парсерів для кінцевих зупинок)
            if arr is None:
                arr = dep
            if dep is None:
                dep = arr
            stop_times.append((i * 10, f"rail:{r['code1']}", arr, dep))

        trip_id = f"rail:{train_id}"
        feed.trips[trip_id] = route_id
        feed.stop_times[trip_id] = stop_times

        # Кілька потягів одного маршруту (E1/E2) мають однакову послідовність
        # станцій — досить одного shape на маршрут, не на кожен потяг.
        shape_id = f"{route_id}:shape"
        feed.trip_shape[trip_id] = shape_id
        if shape_id not in feed.shapes:
            feed.shapes[shape_id] = [
                (i * 10, feed.stops[f"rail:{r['code1']}"][0], feed.stops[f"rail:{r['code1']}"][1])
                for i, r in enumerate(rows)
            ]
        n_included += 1

    print(f"[rail] {len(stops)} платформ (реальні координати з {stops_path}), "
          f"{n_included} потягів включено ({day_type}), "
          f"{n_skipped_daytype} пропущено через тип обслуговування (тільки будні), "
          f"{n_skipped_short} пропущено (замало впізнаних зупинок)")
    return feed


# ---------------------------------------------------------------------------
# Побудова GtfsFeed для метро + фунікулера
# ---------------------------------------------------------------------------
def _build_line_trip(feed, mode_tag, route_id, station_ids, reverse: bool,
                      speed_kmh: float, dwell_s: float):
    """Один шаблонний trip (напрямок forward/reverse) — координати вже є
    в feed.stops. Повертає trip_id і шаблонний stop_times-список
    (сумісний із feed.frequencies + expand_frequencies)."""
    chain = list(reversed(station_ids)) if reverse else station_ids
    direction_tag = "rv" if reverse else "st"
    trip_id = f"{route_id}:{direction_tag}"
    speed_ms = speed_kmh * 1000 / 3600
    t = 0.0
    stop_times = []
    for i, sid in enumerate(chain):
        stop_times.append((i * 10, sid, t, t))
        if i < len(chain) - 1:
            lat1, lng1, _ = feed.stops[chain[i]]
            lat2, lng2, _ = feed.stops[chain[i + 1]]
            dist_m = base.haversine_m(lat1, lng1, lat2, lng2)
            t += dist_m / speed_ms + dwell_s
    feed.trips[trip_id] = route_id
    feed.stop_times[trip_id] = stop_times
    shape_id = f"{route_id}:shape"
    feed.trip_shape[trip_id] = shape_id
    feed.shapes[shape_id] = [(i * 10, feed.stops[s][0], feed.stops[s][1]) for i, s in enumerate(chain)]
    return trip_id


def build_metro_feed(underground_timeperiod_path: str,
                      funicular_timeperiod_path: Optional[str] = None,
                      day_type: str = "weekday") -> "base.GtfsFeed":
    """
    Будує GtfsFeed для 3 ліній метро (+ фунікулер, якщо переданий шлях),
    з РЕАЛЬНИМИ інтервалами руху з timePeriodUnderground.json /
    timePeriodKyivFunicular.json, розгорнутими як frequencies.txt-записи
    (сумісно з expand_frequencies() із kyiv_backend.py).

    day_type: "weekday" або "holiday" — яку колонку інтервалів
    використовувати (st_weekday/rv_weekday чи st_holiday/rv_holiday).
    ПРИМІТКА: у проєкті немає calendar.txt для метро (той самий
    компроміс, що вже задокументований у compute_accessibility_grid() —
    "відсутність фільтрації за calendar.txt"), тому вибір день/вихідний
    тут статичний параметр виклику, а не автоматичне визначення дати.
    """
    assert day_type in ("weekday", "holiday")
    idx_up = 2 if day_type == "weekday" else 4    # st_* колонка
    idx_down = 3 if day_type == "weekday" else 5  # rv_* колонка

    feed = base.GtfsFeed()
    coord_override = load_metro_coords_override()
    n_overridden = 0

    # --- станції ---
    line_station_ids = {}
    for key, line in METRO_LINES.items():
        ids = []
        for name, pt in zip(line["names"], line["pts"]):
            sid = f"metro:{key}:{name}"
            if sid in coord_override:
                feed.stops[sid] = coord_override[sid]
                n_overridden += 1
            else:
                feed.stops[sid] = (pt[0], pt[1], name)
            ids.append(sid)
        line_station_ids[key] = ids

    # --- маршрути + trips + frequencies (з реальних інтервалів) ---
    periods_by_line = load_underground_time_periods(underground_timeperiod_path)
    n_periods_used = 0
    for official_name, key in LINE_NAME_TO_KEY.items():
        line = METRO_LINES[key]
        route_id = f"metro:{key}"
        feed.routes[route_id] = (line["official_name"], "1")  # GTFS route_type 1 = subway/metro

        trip_fwd = _build_line_trip(feed, "metro", route_id, line_station_ids[key],
                                     reverse=False, speed_kmh=METRO_SPEED_KMH, dwell_s=METRO_DWELL_S)
        trip_rev = _build_line_trip(feed, "metro", route_id, line_station_ids[key],
                                     reverse=True, speed_kmh=METRO_SPEED_KMH, dwell_s=METRO_DWELL_S)

        periods = periods_by_line.get(official_name, [])
        freq_fwd, freq_rev = [], []
        for entry in periods:
            start_s, end_s = entry[0], entry[1]
            headway_up_s = entry[idx_up]
            headway_down_s = entry[idx_down]
            freq_fwd.append((start_s, end_s, headway_up_s))
            freq_rev.append((start_s, end_s, headway_down_s))
            n_periods_used += 1
        if freq_fwd:
            feed.frequencies[trip_fwd] = freq_fwd
            feed.frequencies[trip_rev] = freq_rev
        else:
            print(f"[metro] УВАГА: не знайдено інтервалів для лінії '{official_name}' "
                  f"у {underground_timeperiod_path} — рейси НЕ будуть розгорнуті "
                  f"(станції додані, але лінія буде недосяжна в /isochrone)")

    print(f"[metro] {sum(len(v) for v in line_station_ids.values())} станцій, "
          f"3 лінії, {n_periods_used} погодинних інтервалів застосовано ({day_type})"
          + (f", {n_overridden} координат підмінено з {METRO_COORDS_OVERRIDE_PATH}" if n_overridden else ""))

    # --- фунікулер ---
    if funicular_timeperiod_path:
        fun_ids = []
        for name, pt in zip(FUNICULAR_NAMES, FUNICULAR_PTS):
            sid = f"funicular:{name}"
            if sid in coord_override:
                feed.stops[sid] = coord_override[sid]
                n_overridden += 1
            else:
                feed.stops[sid] = (pt[0], pt[1], name)
            fun_ids.append(sid)

        route_id = "funicular:main"
        feed.routes[route_id] = ("Київський фунікулер", "7")  # GTFS route_type 7 = funicular

        trip_up = _build_line_trip(feed, "funicular", route_id, fun_ids,
                                    reverse=False, speed_kmh=1, dwell_s=0)
        # фунікулер — фіксована тривалість рейсу (трос, не швидкість), а не
        # відстань/швидкість; перезаписуємо шаблонний stop_times під це
        feed.stop_times[trip_up] = [(0, fun_ids[0], 0, 0), (10, fun_ids[1], FUNICULAR_TRIP_S, FUNICULAR_TRIP_S)]
        trip_down = _build_line_trip(feed, "funicular", route_id, fun_ids,
                                      reverse=True, speed_kmh=1, dwell_s=0)
        feed.stop_times[trip_down] = [(0, fun_ids[1], 0, 0), (10, fun_ids[0], FUNICULAR_TRIP_S, FUNICULAR_TRIP_S)]

        periods = load_funicular_time_periods(funicular_timeperiod_path)
        freq_up = [(p[0], p[1], p[idx_up]) for p in periods]
        freq_down = [(p[0], p[1], p[idx_down]) for p in periods]
        feed.frequencies[trip_up] = freq_up
        feed.frequencies[trip_down] = freq_down
        print(f"[funicular] 2 станції, {len(periods)} погодинних інтервалів застосовано ({day_type})")

    # --- довідкові пересадки (без координат, див. докстрінг) ---
    feed.transfer_hints = []  # type: ignore[attr-defined]
    return feed


def build_and_merge_into(base_feed: "base.GtfsFeed",
                          underground_timeperiod_path: str = "timePeriodUnderground.json",
                          funicular_timeperiod_path: str = "timePeriodKyivFunicular.json",
                          interchange_path: Optional[str] = "stopsInterchangeUnderground.json",
                          rail_stops_path: Optional[str] = "stopsKyivCityExpress.geojson",
                          rail_stoptimes_path: Optional[str] = "stopTimesKyivCityExpress.json",
                          day_type: str = "weekday") -> "base.GtfsFeed":
    """Зручна функція для create_app(): будує метро+фунікулер+електричку і
    зливає в основний GTFS-фід (трамвай/тролейбус/автобус) через
    merge_feeds().

    Метро/фунікулер розгортаються через expand_frequencies() (лише
    погодинний headway у джерелі). Електричка (rail) — окремо, ПІСЛЯ
    expand_frequencies(): у неї вже реальні per-train stop_times, і
    зайвий прохід expand_frequencies() по ній не потрібен (і нешкідливий:
    у rail_feed.frequencies просто нема записів для видалення)."""
    metro_feed = build_metro_feed(underground_timeperiod_path, funicular_timeperiod_path, day_type)
    merged = base.merge_feeds(base_feed, metro_feed)
    merged.frequencies.update(metro_feed.frequencies)
    base.expand_frequencies(merged)

    if interchange_path:
        try:
            transfers = load_interchange_transfers(interchange_path)
            print(f"[metro] {len(transfers)} пересадок метро<->наземний транспорт завантажено "
                  f"як довідка (координат немає — див. докстрінг модуля щодо подальшого "
                  f"зіставлення по назві з GTFS Київпастрансу)")
        except FileNotFoundError:
            pass

    if rail_stops_path and rail_stoptimes_path:
        try:
            rail_feed = build_rail_feed(rail_stops_path, rail_stoptimes_path, day_type)
            merged = base.merge_feeds(merged, rail_feed)
            print(f"[rail] електричку долучено до спільного фіда: {len(rail_feed.stops)} "
                  f"платформ, {len(rail_feed.trips)} рейсів (реальні координати й розклад)")
        except FileNotFoundError as e:
            print(f"[warn] електричку пропущено — не знайдено файл: {e}")

    return merged


if __name__ == "__main__":
    # Автономна перевірка парсингу й побудови фіда БЕЗ street_graph/osmnx —
    # корисно запускати окремо, щоб швидко побачити, чи коректно
    # розпізнались реальні дані, перш ніж піднімати весь важкий бекенд.
    feed = build_metro_feed("timePeriodUnderground.json", "timePeriodKyivFunicular.json", day_type="weekday")
    base.expand_frequencies(feed)
    n_trips = len(feed.stop_times)
    n_stops = len(feed.stops)
    print(f"\n[selftest] метро+фунікулер: станцій {n_stops}, синтетичних рейсів після розгортання {n_trips}")
    sample_trip = next(iter(feed.stop_times))
    print(f"[selftest] приклад рейсу {sample_trip}: {feed.stop_times[sample_trip][:3]} ...")

    rail_feed = build_rail_feed("stopsKyivCityExpress.geojson", "stopTimesKyivCityExpress.json", day_type="weekday")
    print(f"\n[selftest] електричка: {len(rail_feed.stops)} платформ, "
          f"{len(rail_feed.trips)} потягів (weekday), {len(rail_feed.routes)} маршрутів")
    rail_feed_weekend = build_rail_feed("stopsKyivCityExpress.geojson", "stopTimesKyivCityExpress.json", day_type="weekend")
    print(f"[selftest] електричка: {len(rail_feed_weekend.trips)} потягів (weekend, "
          f"має бути менше — виключені 'крім сб., нд.' рейси)")
    sample_rail_trip = next(iter(rail_feed.stop_times))
    print(f"[selftest] приклад рейсу електрички {sample_rail_trip}: {rail_feed.stop_times[sample_rail_trip][:3]} ...")
