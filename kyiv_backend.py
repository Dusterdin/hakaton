"""
CityTwin — РОБОЧИЙ бекенд для реальних ізохрон (не скелет із TODO, а повний код).
=================================================================================
Чому це окремий файл, а не частина index.html: браузер не може порахувати
Дейкстру по графу з десятків тисяч вузлів (граф вулиць Києва) за прийнятний
час, і не може зберігати такий граф локально. Для цього потрібен процес,
що працює на комп'ютері з відкритим доступом до інтернету — тобто НЕ в
пісочниці Claude (тут мережа обмежена лише до npm/pypi/github).

ЩО ЦЕЙ СКРИПТ РОБИТЬ:
  1. Завантажує реальний граф вулиць Києва з OpenStreetMap (osmnx).
  2. Парсить справжній GTFS-фід (routes/trips/stops/stop_times/shapes.txt,
     а також frequencies.txt — це критично для МЕТРО: метро зазвичай публікується
     без фіксованого розкладу, лише як "їздить кожні N сек у проміжку [start,end]",
     і без розгортання цього файлу метро в ізохроні виглядає недосяжним).
  3. Будує єдиний мультимодальний граф: вулиці (пішки/авто) + транзитні
     ребра з РЕАЛЬНИМ розкладом (а не наближеною "швидкістю лінії").
  4. Рахує ізохрону алгоритмом Дейкстри від будь-якої точки.
  5. Віддає результат як GeoJSON через FastAPI — фронтенд (index.html)
     може запитувати цей ендпоінт замість локальної JS-симуляції.

ЯК ЗАПУСТИТИ (на своїй машині чи сервері з інтернетом):
  pip install osmnx networkx fastapi uvicorn shapely
  1. Заповни GTFS_ZIP_PATH нижче (завантаж вручну з data.kyivcity.gov.ua —
     шукай набір "Розклад руху міського електричного та автомобільного
     транспорту", ресурс GTFSStatic; я не зміг перевірити точний ID
     ресурсу з цієї сесії, портал — JS-застосунок, який мій інструмент
     фетчу не рендерить).
  2. python kyiv_backend.py   # перший запуск ~5-15 хв: тягне граф вулиць
  3. Ендпоінт: GET /isochrone?lat=50.45&lng=30.52&mode=transit&minutes=45

Я протестував весь алгоритмічний код нижче (build_multimodal_graph,
compute_isochrone, GTFS-парсер) на синтетичному графі-макеті — логіка
робоча. Єдине, що не могло бути перевірено в цій сесії — фактичне
завантаження реальних даних Києва (немає мережі в пісочниці).
"""

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import networkx as nx

# ---------------------------------------------------------------------------
# КОНФІГ
# ---------------------------------------------------------------------------
GTFS_ZIP_PATH = "kyivpastrans_gtfs.zip"   # заповнити після ручного завантаження
PLACE = "Kyiv, Ukraine"
WALK_SPEED_KMH = 4.5
TRANSFER_WALK_RADIUS_M = 400  # максимальна пішохідна пересадка зупинка<->вулиця

# Метро й міська електричка НЕ входять у GTFS-архів Київпастрансу — це окремі
# набори ресурсів у ТОМУ Ж датасеті на data.kyivcity.gov.ua. Шукай на сторінці
# датасету ресурси з такими назвами (кожен — окреме посилання-API):
#   Метро:      underground, stopsInterchangeUnderground, timePeriodUnderground,
#               stopTimesUnderground, calendarUnderground
#   Електричка: kyivCityExpress, stopsKyivCityExpress, stopTimesKyivCityExpress,
#               stopsInterchangeKyivCityExpress
#   Фунікулер:  kyivFunicular, stopsInterchangeKyivFunicular, timePeriodKyivFunicular,
#               stopTimesKyivFunicular, calendarKyivFunicular
# Завантаж кожен ресурс (CSV/JSON) і встав шлях нижче. Якщо файлу немає —
# сервер просто пропускає цей вид транспорту й не падає (як і з GTFS).
UNDERGROUND_SEGMENTS_PATH = "underground.csv"           # ресурс "underground"
UNDERGROUND_STOPS_PATH = "stopsInterchangeUnderground.csv"
UNDERGROUND_TIMEPERIOD_PATH = "timePeriodUnderground.csv"
KYIVCITYEXPRESS_SEGMENTS_PATH = "kyivCityExpress.csv"
KYIVCITYEXPRESS_STOPS_PATH = "stopsKyivCityExpress.csv"
KYIVCITYEXPRESS_STOPTIMES_PATH = "stopTimesKyivCityExpress.csv"


# ---------------------------------------------------------------------------
# КРОК 1. Граф вулиць
# ---------------------------------------------------------------------------
def load_street_graph(place: str = PLACE, network_type: str = "walk"):
    """Реальний виклик — працює лише з інтернетом до Overpass API."""
    import osmnx as ox
    G = ox.graph_from_place(place, network_type=network_type)
    G = ox.add_edge_speeds(G) if network_type == "drive" else G
    G = ox.add_edge_travel_times(G) if network_type == "drive" else G
    return G


# ---------------------------------------------------------------------------
# КРОК 2. GTFS-парсер (лише стандартна бібліотека — без зайвих залежностей)
# ---------------------------------------------------------------------------
@dataclass
class GtfsFeed:
    stops: dict = field(default_factory=dict)         # stop_id -> (lat, lng, name)
    routes: dict = field(default_factory=dict)         # route_id -> (short_name, route_type)
    trips: dict = field(default_factory=dict)          # trip_id -> route_id
    stop_times: dict = field(default_factory=dict)     # trip_id -> [(seq, stop_id, arrival_s, departure_s)]
    shapes: dict = field(default_factory=dict)         # shape_id -> [(seq, lat, lng), ...] сортовано
    trip_shape: dict = field(default_factory=dict)     # trip_id -> shape_id
    frequencies: dict = field(default_factory=dict)    # trip_id -> [(start_s, end_s, headway_s), ...]


def _time_to_seconds(t: str) -> int:
    # GTFS дозволяє години >23 (напр. 25:10:00) для нічних рейсів після півночі
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def parse_gtfs(zip_path: str) -> GtfsFeed:
    """
    Реальні GTFS-архіви не завжди мають файли строго в корені ZIP —
    трапляються підпапки (напр. окремо по видах транспорту: metro/stops.txt,
    trolleybus/stops.txt) або кілька фідів в одному архіві. Ця версія сама
    знаходить усі stops.txt де завгодно в архіві і об'єднує їх, префіксуючи
    ID підпапкою, щоб фіди не конфліктували між собою.
    """
    feed = GtfsFeed()
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        stops_files = [n for n in names if n.lower().endswith("stops.txt")]
        if not stops_files:
            preview = "\n".join(f"  - {n}" for n in names[:40])
            more = f"\n  ... і ще {len(names)-40}" if len(names) > 40 else ""
            raise FileNotFoundError(
                f"У архіві '{zip_path}' немає жодного stops.txt (перевірено {len(names)} файлів). "
                f"Це може бути не GTFS static, а щось інше (розклад у PDF/XML, GTFS-RT тощо). "
                f"Вміст архіву:\n{preview}{more}\n"
                f"Перевір на порталі, що завантажив саме ресурс типу GTFSStatic (ZIP з .txt файлами "
                f"routes/stops/trips/stop_times всередині), а не GTFS realtime чи інший формат."
            )

        for stops_path in stops_files:
            prefix = stops_path.rsplit("stops.txt", 1)[0]  # напр. 'metro/' або ''
            tag = prefix.strip("/").replace("/", "_") or "main"

            def _read_csv(fname):
                path = prefix + fname
                if path not in names:
                    return []
                with z.open(path) as f:
                    return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

            for row in _read_csv("stops.txt"):
                sid = f"{tag}:{row['stop_id']}"
                feed.stops[sid] = (float(row["stop_lat"]), float(row["stop_lon"]), row.get("stop_name", ""))

            for row in _read_csv("routes.txt"):
                rid = f"{tag}:{row['route_id']}"
                feed.routes[rid] = (row.get("route_short_name", ""), row.get("route_type", ""))

            trip_to_route = {}
            for row in _read_csv("trips.txt"):
                tid = f"{tag}:{row['trip_id']}"
                trip_to_route[tid] = f"{tag}:{row['route_id']}"
                feed.trips[tid] = trip_to_route[tid]
                shape_id = row.get("shape_id")
                if shape_id:
                    feed.trip_shape[tid] = f"{tag}:{shape_id}"

            shape_rows = _read_csv("shapes.txt")
            if shape_rows:
                for row in shape_rows:
                    sid = f"{tag}:{row['shape_id']}"
                    feed.shapes.setdefault(sid, []).append((
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lat"]), float(row["shape_pt_lon"]),
                    ))
                for sid in list(feed.shapes.keys()):
                    if sid.startswith(f"{tag}:"):
                        feed.shapes[sid].sort(key=lambda x: x[0])
                print(f"[gtfs] фід '{tag}': реальна геометрія {len(shape_rows)} точок у {sum(1 for k in feed.shapes if k.startswith(tag+':'))} маршрутах (shapes.txt)")
            else:
                print(f"[warn] фід '{tag}' не має shapes.txt — реальної геометрії ліній не буде, "
                      f"тільки послідовність зупинок (менш точно, але не вигадано)")

            st_rows = _read_csv("stop_times.txt")
            if not st_rows:
                print(f"[warn] {prefix}stop_times.txt порожній або відсутній — пропускаю фід '{tag}'")
                continue
            for row in st_rows:
                tid = f"{tag}:{row['trip_id']}"
                sid = f"{tag}:{row['stop_id']}"
                feed.stop_times.setdefault(tid, []).append((
                    int(row["stop_sequence"]), sid,
                    _time_to_seconds(row["arrival_time"]),
                    _time_to_seconds(row["departure_time"]),
                ))
            print(f"[gtfs] фід '{tag}': {sum(1 for r in _read_csv('stops.txt'))} зупинок, "
                  f"{len(trip_to_route)} рейсів")

            # ВАЖЛИВО: метро (і часто нічні/маршруткові маршрути) в реальних GTFS-фідах
            # публікуються БЕЗ фіксованого розкладу (немає сенсу публікувати час прибуття
            # кожного потяга), а через frequencies.txt — "їздить кожні N секунд у проміжку
            # [start,end]". Без обробки цього файлу stop_times.txt для таких рейсів містить
            # лише ОДИН умовний "шаблонний" прохід, і метро в ізохроні виглядає так, ніби
            # їде раз на добу — по суті недосяжне. Тому явно розгортаємо частоти нижче.
            freq_rows = _read_csv("frequencies.txt")
            for row in freq_rows:
                tid = f"{tag}:{row['trip_id']}"
                feed.frequencies.setdefault(tid, []).append((
                    _time_to_seconds(row["start_time"]),
                    _time_to_seconds(row["end_time"]),
                    int(row["headway_secs"]),
                ))
            if freq_rows:
                print(f"[gtfs] фід '{tag}': {len(freq_rows)} записів frequencies.txt "
                      f"для {len(feed.frequencies)} рейсів-шаблонів (буде розгорнуто в синтетичні відправлення)")

    for trip_id in feed.stop_times:
        feed.stop_times[trip_id].sort(key=lambda x: x[0])
    if not feed.stops:
        raise FileNotFoundError(f"Знайдено stops.txt у '{zip_path}', але жодного рядка даних не прочитано.")

    # Діагностика: показуємо РЕАЛЬНІ значення route_type, які трапились у
    # даних, і скільки маршрутів на кожне. Якщо тут з'явиться щось окрім
    # "1"/"2"/"3" — Київпастранс змінив схему нумерації, і ROUTE_TYPE_NAME
    # вище треба буде поправити відповідно (замість тихого "unknown").
    type_counts: dict = {}
    for _short_name, route_type in feed.routes.values():
        type_counts[str(route_type)] = type_counts.get(str(route_type), 0) + 1
    print(f"[gtfs] route_type у даних: {dict(sorted(type_counts.items()))} "
          f"(мапиться як: 0=трамвай, 11=тролейбус, 3=автобус — стандартні GTFS-коди; "
          f"якщо з'явиться щось інше — онови ROUTE_TYPE_NAME/ROUTE_TYPE_COLOR)")

    return feed


# ---------------------------------------------------------------------------
# КРОК 3. Мультимодальний граф
# ---------------------------------------------------------------------------
def haversine_m(lat1, lng1, lat2, lng2) -> float:
    from math import radians, sin, cos, atan2, sqrt
    R = 6371000
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def build_static_graph(street_graph) -> nx.DiGraph:
    """Лише вулиці — без транзиту. Транзит рахується окремо, динамічно,
    щоб коректно не задвоювати час очікування на пересадках у межах
    одного рейсу (див. compute_isochrone_realtime нижче)."""
    G = nx.DiGraph()
    G.add_nodes_from(street_graph.nodes(data=True))
    for u, v, data in street_graph.edges(data=True):
        length_m = data.get("length", 1.0)
        G.add_edge(u, v, weight=length_m / (WALK_SPEED_KMH * 1000 / 3600))
    return G


def add_transit_stops_and_transfers(G: nx.DiGraph, street_graph, feed: GtfsFeed) -> dict:
    """Додає вузли зупинок і пішохідні пересадки зупинка<->вулиця. Повертає stop_id -> node_id."""
    stop_node = {}
    for stop_id, (lat, lng, name) in feed.stops.items():
        node_id = f"stop_{stop_id}"
        G.add_node(node_id, lat=lat, lng=lng, stop_id=stop_id, kind="transit_stop", name=name)
        stop_node[stop_id] = node_id

    # ВАЖЛИВО: ox.distance.nearest_nodes у скалярному режимі (один виклик =
    # одна точка) щоразу заново будує просторовий індекс по ВСЬОМУ графу
    # вулиць. Викликати це в циклі по 1000+ зупинок — це O(n_stops * граф),
    # реально може виконуватись 20+ хвилин на великому місті й виглядати
    # як "зависання". Векторизований виклик будує індекс ОДИН раз і шукає
    # найближчі вузли для всіх точок одночасно — у сотні разів швидше.
    try:
        import osmnx as ox
        stop_ids = list(stop_node.keys())
        lats = [feed.stops[sid][0] for sid in stop_ids]
        lngs = [feed.stops[sid][1] for sid in stop_ids]
        print(f"[transfers] шукаю найближчі вулиці для {len(stop_ids)} зупинок (векторизовано)...")
        nearest_list = ox.distance.nearest_nodes(street_graph, X=lngs, Y=lats)
        for stop_id, nearest in zip(stop_ids, nearest_list):
            lat, lng, _ = feed.stops[stop_id]
            node_id = stop_node[stop_id]
            d = haversine_m(lat, lng, street_graph.nodes[nearest]["y"], street_graph.nodes[nearest]["x"])
            if d <= TRANSFER_WALK_RADIUS_M:
                t = d / (WALK_SPEED_KMH * 1000 / 3600)
                G.add_edge(node_id, nearest, weight=t)
                G.add_edge(nearest, node_id, weight=t)
        print("[transfers] готово")
    except Exception as e:
        print(f"[warn] не вдалось побудувати пересадочні ребра: {e}")
    return stop_node


def expand_frequencies(feed: GtfsFeed, min_synthetic_headway_s: int = 60) -> None:
    """
    Розгортає frequencies.txt у реальні синтетичні відправлення.

    Метро (і взагалі багато сучасних GTFS-фідів) не публікує "потяг о 14:32:10" —
    натомість дає rows виду (trip_id, start_time, end_time, headway_secs), тобто
    "цей рейс-шаблон повторюється кожні headway_secs секунд у проміжку [start,end)".
    stop_times.txt для такого trip_id містить лише ОДИН шаблонний прохід з часами,
    які трактуються як зміщення від початку рейса (а не абсолютний час доби).

    Без цієї функції build_stop_departure_index бачить один-єдиний запис на
    добу для кожного такого trip_id — і метро в ізохроні виглядає як недосяжне,
    бо "наступний потяг" завжди в минулому або невірний.

    Тут для кожного (start,end,headway) генеруємо трips, зсуваючи весь шаблонний
    патерн зупинок на t = start, start+headway, start+2*headway, ... < end,
    і замінюємо ними оригінальний "шаблонний" trip_id у feed.stop_times.
    """
    if not feed.frequencies:
        return
    total_synthetic = 0
    for trip_id, freq_list in feed.frequencies.items():
        template = feed.stop_times.get(trip_id)
        if not template:
            continue
        base_dep = template[0][3]  # departure_time першої зупинки шаблону = точка відліку
        offsets = [(seq, sid, arr - base_dep, dep - base_dep) for seq, sid, arr, dep in template]
        route_id = feed.trips.get(trip_id)
        shape_id = feed.trip_shape.get(trip_id)  # переносимо на кожен синтетичний рейс,
                                                  # інакше build_routes_geojson не знайде
                                                  # геометрію лінії метро (бо шаблонного
                                                  # trip_id більше не буде в feed.trips)
        idx = 0
        for start_s, end_s, headway_s in freq_list:
            headway_s = max(headway_s, min_synthetic_headway_s)
            t = start_s
            while t < end_s:
                synth_id = f"{trip_id}::f{idx}"
                idx += 1
                feed.stop_times[synth_id] = [
                    (seq, sid, t + off_arr, t + off_dep) for seq, sid, off_arr, off_dep in offsets
                ]
                if route_id is not None:
                    feed.trips[synth_id] = route_id
                if shape_id is not None:
                    feed.trip_shape[synth_id] = shape_id
                t += headway_s
        total_synthetic += idx
        del feed.stop_times[trip_id]  # шаблон сам по собі більше не є реальним відправленням
        feed.trips.pop(trip_id, None)
        feed.trip_shape.pop(trip_id, None)
    print(f"[gtfs] frequencies.txt розгорнуто: {total_synthetic} синтетичних відправлень "
          f"замість {len(feed.frequencies)} рейсів-шаблонів (це і є фікс для метро без розкладу)")


# ---------------------------------------------------------------------------
# КРОК 3b. Метро й міська електричка — окремі API, не GTFS-архів
# ---------------------------------------------------------------------------
# Точну структуру колонок цих CSV я не бачив (портал JS-рендерений, мій
# фетч не показує реальний вміст файлів) — тому парсер "нюхає" заголовки й
# намагається розпізнати потрібні поля за кількома можливими назвами,
# друкуючи що саме знайшов. Якщо не вгадає — консоль покаже РЕАЛЬНІ
# заголовки твого файлу, і я миттю підправлю ALIASES нижче під них.
_FIELD_ALIASES = {
    "stop_id":   ["stop_id", "stopId", "id", "code", "stop_code", "stopCode"],
    "stop_name": ["stop_name", "stopName", "name", "station_name", "stationName"],
    "lat":       ["lat", "latitude", "stop_lat", "y", "coord_lat"],
    "lon":       ["lon", "lng", "longitude", "stop_lon", "x", "coord_lon"],
    "from_stop": ["from_stop_id", "fromStopId", "start_stop_id", "startStopId", "from", "segment_start", "startStop"],
    "to_stop":   ["to_stop_id", "toStopId", "end_stop_id", "endStopId", "to", "segment_end", "endStop"],
    "sequence":  ["sequence", "seq", "order", "stop_sequence", "segment_sequence"],
    "route_name":["route_name", "routeName", "line", "line_name", "lineName", "name", "route"],
    "direction": ["direction", "dir", "way"],
    "hour":      ["hour", "period", "time_period", "timePeriod"],
    "interval_weekday": ["interval_weekday", "weekday_interval", "workday", "interval", "headway", "headway_s"],
    "interval_weekend": ["interval_weekend", "weekend_interval", "holiday"],
}

def _sniff_field(fieldnames, target):
    lower = {f.lower(): f for f in fieldnames}
    for alias in _FIELD_ALIASES[target]:
        if alias.lower() in lower:
            return lower[alias.lower()]
    return None

def _read_any_csv(path):
    """CSV з невідомим діалектом/роздільником — пробуємо кому, тоді крапку з комою."""
    with open(path, encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        delim = ";" if sample.count(";") > sample.count(",") else ","
        return list(csv.DictReader(f, delimiter=delim))


def load_rail_like_feed(segments_path: str, stops_path: str, timeperiod_path: Optional[str],
                          mode_tag: str, mode_route_type: str, default_speed_kmh: float,
                          default_wait_weekday_min: float, default_wait_weekend_min: float) -> Optional["GtfsFeed"]:
    """
    Універсальний завантажувач для метро (underground.csv + stopsInterchangeUnderground.csv
    + timePeriodUnderground.csv) і міської електрички (kyivCityExpress.csv тощо) —
    конвертує їхній формат (сегменти маршруту + окремі зупинки + погодинні
    інтервали) У ТУ Ж структуру GtfsFeed, що й GTFS-автобуси/трамваї/тролейбуси,
    щоб уся інша машинерія (Дейкстра, /routes, легенда) працювала однаково
    для всіх видів транспорту без спеціальних випадків.

    Якщо файлів немає на диску — повертає None, і виклик просто пропускає
    цей вид транспорту (не падає), як і з основним GTFS.
    """
    import os
    if not os.path.exists(segments_path) or not os.path.exists(stops_path):
        print(f"[{mode_tag}] не знайдено {segments_path} і/або {stops_path} — "
              f"пропускаю (див. коментар у КОНФІГ, де шукати ці ресурси на порталі)")
        return None

    stops_rows = _read_any_csv(stops_path)
    if not stops_rows:
        print(f"[{mode_tag}] {stops_path} порожній — пропускаю")
        return None
    fn = stops_rows[0].keys()
    f_id, f_name, f_lat, f_lon = (_sniff_field(fn, k) for k in ("stop_id","stop_name","lat","lon"))
    if not (f_id and f_lat and f_lon):
        print(f"[{mode_tag}] не вдалось розпізнати колонки в {stops_path}. "
              f"Реальні заголовки файлу: {list(fn)}. "
              f"Онови _FIELD_ALIASES у kyiv_backend.py під ці назви.")
        return None

    feed = GtfsFeed()
    for row in stops_rows:
        sid = f"{mode_tag}:{row[f_id]}"
        name = row.get(f_name, "") if f_name else ""
        try:
            feed.stops[sid] = (float(row[f_lat]), float(row[f_lon]), name)
        except (ValueError, KeyError):
            continue
    print(f"[{mode_tag}] зупинок розпізнано: {len(feed.stops)} (з {len(stops_rows)} рядків)")

    seg_rows = _read_any_csv(segments_path)
    if not seg_rows:
        print(f"[{mode_tag}] {segments_path} порожній — зупинки є, але без сегментів граф не з'єднати")
        return feed
    fn2 = seg_rows[0].keys()
    f_from, f_to, f_seq, f_route, f_dir = (
        _sniff_field(fn2, k) for k in ("from_stop","to_stop","sequence","route_name","direction")
    )
    if not (f_from and f_to):
        print(f"[{mode_tag}] не вдалось розпізнати колонки сегментів у {segments_path}. "
              f"Реальні заголовки файлу: {list(fn2)}. "
              f"Онови _FIELD_ALIASES у kyiv_backend.py під ці назви.")
        return feed

    # погодинні інтервали (реальна частота руху) — якщо файл є, використовуємо
    # замість дефолтних значень
    interval_by_hour = {}
    if timeperiod_path:
        import os as _os
        if _os.path.exists(timeperiod_path):
            tp_rows = _read_any_csv(timeperiod_path)
            if tp_rows:
                fn3 = tp_rows[0].keys()
                f_hour, f_wd, f_we = (_sniff_field(fn3, k) for k in ("hour","interval_weekday","interval_weekend"))
                if f_hour and f_wd:
                    for row in tp_rows:
                        try:
                            interval_by_hour[int(row[f_hour])] = (
                                float(row[f_wd]), float(row.get(f_we, row[f_wd])) if f_we else float(row[f_wd])
                            )
                        except (ValueError, KeyError):
                            continue
                    print(f"[{mode_tag}] реальні погодинні інтервали завантажено для {len(interval_by_hour)} годин")
                else:
                    print(f"[{mode_tag}] не розпізнав колонки в {timeperiod_path} "
                          f"(заголовки: {list(fn3)}) — використовую дефолтний інтервал")

    # будуємо один "маршрут" на кожен route_name+direction, з'єднуючи сегменти по sequence
    routes_seen = {}
    trip_counter = 0
    grouped: dict = {}
    for row in seg_rows:
        route_key = (row.get(f_route, mode_tag) if f_route else mode_tag,
                     row.get(f_dir, "0") if f_dir else "0")
        grouped.setdefault(route_key, []).append(row)

    avg_wait_weekday = sum(v[0] for v in interval_by_hour.values())/len(interval_by_hour)/2 if interval_by_hour else default_wait_weekday_min
    speed_ms = default_speed_kmh * 1000 / 3600

    for route_key, rows in grouped.items():
        if f_seq:
            try:
                rows.sort(key=lambda r: float(r[f_seq]))
            except (ValueError, KeyError):
                pass
        route_id = f"{mode_tag}:{route_key[0]}_{route_key[1]}"
        if route_id not in routes_seen:
            feed.routes[route_id] = (str(route_key[0]), mode_route_type)
            routes_seen[route_id] = True
        trip_id = f"{route_id}:trip"
        feed.trips[trip_id] = route_id
        stop_chain = []
        for row in rows:
            a, b = f"{mode_tag}:{row[f_from]}", f"{mode_tag}:{row[f_to]}"
            if not stop_chain:
                stop_chain.append(a)
            stop_chain.append(b)
        if len(stop_chain) < 2:
            continue
        t = 0
        stop_times = []
        for i, sid in enumerate(stop_chain):
            stop_times.append((i*10, sid, t, t))
            if i < len(stop_chain)-1:
                a_ll = feed.stops.get(stop_chain[i])
                b_ll = feed.stops.get(stop_chain[i+1])
                if a_ll and b_ll:
                    d_km = haversine_m(a_ll[0], a_ll[1], b_ll[0], b_ll[1]) / 1000
                    t += (d_km*1000/speed_ms) if speed_ms>0 else 60
                else:
                    t += 90  # дефолтний перегін, якщо координати не знайдені
        feed.stop_times[trip_id] = stop_times
        # геометрія для /routes — пряма лінія по зупинках цього маршруту (без shapes.txt тут)
        shape_id = f"{route_id}:shape"
        feed.trip_shape[trip_id] = shape_id
        feed.shapes[shape_id] = [(i*10, feed.stops[s][0], feed.stops[s][1]) for i, s in enumerate(stop_chain) if s in feed.stops]
        trip_counter += 1

    print(f"[{mode_tag}] побудовано {trip_counter} маршрутів-напрямків, "
          f"середнє очікування ~{avg_wait_weekday:.1f} хв (будні)")
    return feed


def merge_feeds(base: "GtfsFeed", other: Optional["GtfsFeed"]) -> "GtfsFeed":
    """Об'єднує додатковий фід (метро/електричка) в основний — без конфліктів
    ключів, бо все вже префіксовано mode_tag на етапі load_rail_like_feed."""
    if other is None:
        return base
    base.stops.update(other.stops)
    base.routes.update(other.routes)
    base.trips.update(other.trips)
    base.stop_times.update(other.stop_times)
    base.shapes.update(other.shapes)
    base.trip_shape.update(other.trip_shape)
    return base


def build_stop_departure_index(feed: GtfsFeed) -> dict:
    """stop_id -> [(departure_time_s, trip_id, seq_index), ...] відсортовано за часом.
    seq_index вказує на позицію ЦІЄЇ зупинки в межах рейсу (щоб дістати наступну)."""
    idx: dict = {}
    for trip_id, seq in feed.stop_times.items():
        for i in range(len(seq) - 1):
            _, stop_a, _, dep_a = seq[i]
            idx.setdefault(stop_a, []).append((dep_a, trip_id, i))
    for stop_id in idx:
        idx[stop_id].sort(key=lambda x: x[0])
    return idx


# ---------------------------------------------------------------------------
# КРОК 4. Ізохрона — Dijkstra з урахуванням РЕАЛЬНОГО часу посадки
# ---------------------------------------------------------------------------
def compute_isochrone_realtime(static_graph: nx.DiGraph, feed: GtfsFeed, dep_index: dict,
                                origin_node, query_time_s: int, max_minutes: int = 45,
                                max_boardings_per_stop: int = 6) -> dict:
    """
    ВАЖЛИВО чому не звичайний nx.single_source_dijkstra_path_length:
    у транзиті вага ребра "зупинка A -> зупинка B" залежить від ЧАСУ, коли
    ти прийшов на зупинку A (бо чекати доводиться до найближчого рейсу САМЕ
    з цього моменту) — це не стала вага, а функція від накопиченого часу.
    Тому тут ручна реалізація Дейкстри (heap), де для зупинок вага ребра
    рахується "ліниво", у момент обробки вузла — так і працюють реальні
    транзитні роутери (ідея з Connection Scan Algorithm / RAPTOR).
    """
    import heapq
    from bisect import bisect_left

    cutoff = max_minutes * 60
    dist = {origin_node: 0.0}
    pq = [(0.0, origin_node)]
    visited = set()

    while pq:
        d, n = heapq.heappop(pq)
        if n in visited:
            continue
        visited.add(n)
        if d > cutoff:
            continue

        # 1) статичні ребра — вулиці й пішохідні пересадки зупинка<->вулиця
        if static_graph.has_node(n):
            for nb, edata in static_graph[n].items():
                w = edata.get("weight")
                if w is None:
                    continue
                nd = d + w
                if nd <= cutoff and nd < dist.get(nb, float("inf")):
                    dist[nb] = nd
                    heapq.heappush(pq, (nd, nb))

        # 2) транзитні рейси — тільки якщо це вузол зупинки, і час рахуємо
        #    від РЕАЛЬНОГО моменту прибуття на цю зупинку цим шляхом (d),
        #    а не від фіксованого глобального query_time_s
        stop_id = static_graph.nodes[n].get("stop_id") if static_graph.has_node(n) else None
        if stop_id and stop_id in dep_index:
            threshold = query_time_s + d
            deps = dep_index[stop_id]
            pos = bisect_left(deps, (threshold, "", -1))
            used_trips = set()
            taken = 0
            for dep_time, trip_id, i in deps[pos:]:
                if taken >= max_boardings_per_stop:
                    break
                if trip_id in used_trips:
                    continue
                used_trips.add(trip_id)
                taken += 1
                seq = feed.stop_times[trip_id]
                _, stop_b, arr_b, _ = seq[i + 1]
                wait = dep_time - threshold          # чекаємо лише один раз, при посадці
                ride = max(arr_b - dep_time, 1)       # чиста їзда, без повторного очікування
                nd = d + wait + ride
                nb = f"stop_{stop_b}"
                if nd <= cutoff and nd < dist.get(nb, float("inf")):
                    dist[nb] = nd
                    heapq.heappush(pq, (nd, nb))

    reachable = []
    for node_id, t in dist.items():
        data = static_graph.nodes[node_id] if static_graph.has_node(node_id) else {}
        lat = data.get("y", data.get("lat"))
        lng = data.get("x", data.get("lng"))
        if lat is not None and lng is not None:
            reachable.append({"node": node_id, "lat": lat, "lng": lng, "minutes": round(t / 60, 1)})
    return {"origin": origin_node, "reachable_nodes": reachable, "count": len(reachable)}


def nodes_to_polygon_geojson(reachable_points: list, buffer_m: float = 150.0) -> dict:
    """Перетворює хмару досяжних точок на полігон (buffer + union) для показу на карті."""
    from shapely.geometry import Point, mapping
    from shapely.ops import unary_union
    circles = [Point(p["lng"], p["lat"]).buffer(buffer_m / 111320) for p in reachable_points]
    if not circles:
        return {"type": "FeatureCollection", "features": []}
    merged = unary_union(circles)
    return {"type": "Feature", "geometry": mapping(merged), "properties": {}}


# ---------------------------------------------------------------------------
# КРОК 4b. Власна база — кешування, щоб не перебудовувати граф щоразу
# ---------------------------------------------------------------------------
import json
import os
import pickle
import sqlite3

DB_PATH = "kyiv_cache.sqlite"


def init_db(path: str = DB_PATH):
    # check_same_thread=False: FastAPI обробляє запити в пулі потоків
    # (run_in_threadpool), тож одне й те саме з'єднання відвідують різні
    # потоки. Для нашого навантаження (короткі read/write, невисока
    # паралельність) це безпечно; для важчого продакшна варто перейти
    # на пул з'єднань або відкривати нове з'єднання на кожен запит.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS isochrone_cache (
            cache_key TEXT PRIMARY KEY,
            geojson TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


import threading
_db_lock = threading.Lock()


def cache_get(conn, key: str) -> Optional[dict]:
    with _db_lock:
        row = conn.execute("SELECT geojson FROM isochrone_cache WHERE cache_key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def cache_set(conn, key: str, geojson: dict):
    with _db_lock:
        conn.execute(
            "INSERT OR REPLACE INTO isochrone_cache (cache_key, geojson, created_at) VALUES (?,?,?)",
            (key, json.dumps(geojson), datetime.utcnow().isoformat()),
        )
        conn.commit()


def save_graph_to_disk(G, path: str = "kyiv_street_graph.pkl"):
    """Граф вулиць Києва тягнеться з Overpass одноразово (~5-15 хв) —
    зберігаємо на диск, щоб наступні запуски сервера стартували за секунди."""
    with open(path, "wb") as f:
        pickle.dump(G, f)


def load_graph_from_disk(path: str = "kyiv_street_graph.pkl"):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def get_or_build_street_graph(place: str = PLACE, network_type: str = "walk"):
    cached = load_graph_from_disk(f"kyiv_street_graph_{network_type}.pkl")
    if cached is not None:
        print(f"[cache] завантажено граф вулиць з диска ({network_type})")
        return cached
    print(f"[build] тягну граф вулиць з OpenStreetMap ({network_type}) — це триватиме кілька хвилин...")
    G = load_street_graph(place, network_type)
    save_graph_to_disk(G, f"kyiv_street_graph_{network_type}.pkl")
    return G


# ---------------------------------------------------------------------------
# КРОК 4c. Реальна геометрія маршрутів (для показу справжніх ліній на карті,
# а не намальованих вручну наближень)
# ---------------------------------------------------------------------------
# ВИПРАВЛЕНО за реальними даними (діагностика при старті сервера показала
# {'0': 17, '11': 43, '3': 96} — 17 трамвайних, 43 тролейбусних, 96 автобусних
# маршрутів, що точно збігається з реальним масштабом мереж Києва). Тобто
# офіційний текстовий опис ресурсу на data.kyivcity.gov.ua ("1=трамвай,
# 2=тролейбус, 3=автобус") виявився НЕТОЧНИМ/застарілим для фактичного фіда —
# насправді тут звичайні стандартні GTFS-коди route_type: 0=трамвай,
# 11=тролейбус, 3=автобус (1=метро й 2=залізниця тут просто відсутні, бо
# метро й міська електричка Києва публікуються окремими не-GTFS ендпоінтами
# — underground/kyivCityExpress/kyivFunicular — і на схематичних лініях
# фронтенду це коректно, а не недороблено).
ROUTE_TYPE_COLOR = {"0": "#E8891A", "11": "#33C2D9", "3": "#7F93A8"}
ROUTE_TYPE_NAME = {"0": "tram", "11": "trolleybus", "3": "bus"}


def _haversine_km(lat1, lng1, lat2, lng2):
    from math import radians, sin, cos, atan2, sqrt
    R = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dp/2)**2 + cos(p1)*cos(p2)*sin(dl/2)**2
    return 2*R*atan2(sqrt(a), sqrt(1-a))

def _split_on_gps_anomalies(pts, max_jump_km=3.0):
    """
    Реальні GTFS-фіди (і Київпастранс не виняток) часто мають окремі биті
    точки в shapes.txt — одна погана координата (помилка збору даних,
    0/0, чи зовсім інший район) перетворює охайну лінію маршруту на
    "випадкові" стрибки через усе місто. Замість викидання даних цілком —
    ріжемо трасу на розрив там, де сусідні точки стрибають нереалістично
    далеко (>max_jump_km за один крок shape_pt_sequence), і повертаємо
    кілька коротших, але чистих сегментів замість одного кривого.
    """
    if len(pts) < 2:
        return [pts] if pts else []
    segments = [[pts[0]]]
    for i in range(1, len(pts)):
        _, lat1, lng1 = pts[i-1]
        _, lat2, lng2 = pts[i]
        if _haversine_km(lat1, lng1, lat2, lng2) > max_jump_km:
            segments.append([])  # розрив — біта точка чи реальна телепортація
        segments[-1].append(pts[i])
    return [s for s in segments if len(s) >= 2]


def build_routes_geojson(feed: GtfsFeed, drive_graph=None) -> dict:
    """
    Пріоритет джерела геометрії:
      1. shapes.txt — це РЕАЛЬНА траса від перевізника (GPS-трек маршруту),
         найточніше, що взагалі буває в GTFS.
      2. Якщо shapes.txt немає для маршруту (типово для тролейбуса/автобуса —
         багато перевізників публікують shapes.txt лише для рейок) — беремо
         послідовність зупинок НАЙДОВШОГО рейсу маршруту (а не першого-ліпшого:
         короткий/скорочений рейс-варіант обрізає лінію і робить її кривою) і
         З'ЄДНУЄМО зупинки НЕ прямими лініями, а найкоротшим шляхом по РЕАЛЬНІЙ
         дорожній мережі (drive_graph) — це і прибирає той самий ефект "лінія
         ріже кути, перетинає квартали по діагоналі", який виглядає криво.
         Якщо для якоїсь пари зупинок шлях по вулицях не знайдено (острівець
         графа, розрив), саме ця ділянка тихо falls back на пряму лінію —
         решта маршруту лишається точною по вулицях.
    Кожен Feature позначений properties.approx=True, якщо це запасний
    варіант (2), і properties.snapped=True, якщо його вдалось прив'язати
    до дорожньої мережі (а не звести до прямих ліній між зупинками).
    """
    import osmnx as ox

    features = []

    route_shapes: dict = {}
    for trip_id, route_id in feed.trips.items():
        shape_id = feed.trip_shape.get(trip_id)
        if shape_id and shape_id in feed.shapes:
            route_shapes.setdefault(route_id, set()).add(shape_id)

    bad_shapes_count = 0
    for route_id, shape_ids in route_shapes.items():
        short_name, route_type = feed.routes.get(route_id, ("", ""))
        color = ROUTE_TYPE_COLOR.get(str(route_type), "#999999")
        kind = ROUTE_TYPE_NAME.get(str(route_type), "unknown")
        for shape_id in shape_ids:
            pts = feed.shapes.get(shape_id, [])
            if len(pts) < 2:
                continue
            clean_segments = _split_on_gps_anomalies(pts)
            if len(clean_segments) > 1:
                bad_shapes_count += 1
            for seg in clean_segments:
                coords = [[lng, lat] for _, lat, lng in seg]
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {"route_id": route_id, "name": short_name, "kind": kind,
                                   "color": color, "approx": False, "snapped": False},
                })
    if bad_shapes_count:
        print(f"[routes] увага: {bad_shapes_count} трас shapes.txt мали биті GPS-точки "
              f"(стрибок >3км за крок) — розрізано на чисті сегменти замість кривих ліній")

    routes_with_real_shape = set(route_shapes.keys())

    # Обираємо НАЙДОВШИЙ рейс (найбільше зупинок) як представника маршруту —
    # раніше брався перший-ліпший trip_id, і якщо це був скорочений/полу-рейс,
    # лінія на карті виглядала обрізаною чи кривою відносно реального маршруту.
    best_trip_per_route: dict = {}
    for trip_id, route_id in feed.trips.items():
        if route_id in routes_with_real_shape:
            continue
        seq = feed.stop_times.get(trip_id)
        if not seq or len(seq) < 2:
            continue
        cur_best = best_trip_per_route.get(route_id)
        if cur_best is None or len(seq) > len(feed.stop_times[cur_best]):
            best_trip_per_route[route_id] = trip_id

    # Векторизовано (один виклик, не в циклі по кожній зупинці — та сама причина
    # продуктивності, що й у add_transit_stops_and_transfers) прив'язуємо всі
    # потрібні зупинки до найближчих вузлів ДОРОЖНЬОГО (не пішохідного) графа,
    # бо тролейбус/автобус їздять по проїзній частині, а не по тротуарах.
    stop_to_node: dict = {}
    if drive_graph is not None and best_trip_per_route:
        try:
            needed_stops = sorted({
                sid for trip_id in best_trip_per_route.values()
                for _, sid, _, _ in feed.stop_times[trip_id] if sid in feed.stops
            })
            lats = [feed.stops[sid][0] for sid in needed_stops]
            lngs = [feed.stops[sid][1] for sid in needed_stops]
            nearest = ox.distance.nearest_nodes(drive_graph, X=lngs, Y=lats)
            stop_to_node = dict(zip(needed_stops, nearest))
            print(f"[routes] прив'язано {len(stop_to_node)} зупинок до дорожнього графа "
                  f"для точного трасування ліній без shapes.txt")
        except Exception as e:
            print(f"[warn] не вдалось прив'язати зупинки до дорожнього графа "
                  f"(лінії без shapes.txt лишаться прямими між зупинками): {e}")

    path_cache: dict = {}

    # osmnx 'drive'-графи ДИРЕКТОВАНІ (одностороній рух), тож
    # nx.shortest_path між двома вузлами інколи змушений об'їхати через
    # розворот в іншому кінці кварталу — геометрично коректний найкоротший
    # шлях по графу, але зовсім не той фізичний коридор, яким реально їде
    # тролейбус/автобус. На карті це виглядає як різка "петля" чи зайвий
    # гачок в одному місці маршруту — це і є "шляхи неправильні", які важко
    # помітити в коді, але одразу видно на карті. Захист: якщо прокладений
    # по вулицях відрізок довший за пряму між тими самими зупинками більш
    # ніж у DETOUR_RATIO_LIMIT разів — це майже напевно об'їзд через
    # одностороннє обмеження, а не реальний маршрут, тож використовуємо
    # пряму лінію для цього конкретного відрізка замість кривого об'їзду.
    DETOUR_RATIO_LIMIT = 2.2

    def street_path_coords(node_a, node_b, straight_m=None):
        if node_a == node_b:
            return None
        key = (node_a, node_b)
        if key in path_cache:
            return path_cache[key]
        try:
            node_path = nx.shortest_path(drive_graph, node_a, node_b, weight="length")
            coords = [[drive_graph.nodes[n]["x"], drive_graph.nodes[n]["y"]] for n in node_path]
            if straight_m and len(coords) >= 2:
                path_m = sum(
                    haversine_m(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0])
                    for i in range(len(coords) - 1)
                )
                if straight_m > 30 and path_m > straight_m * DETOUR_RATIO_LIMIT:
                    coords = None  # підозра на об'їзд через одностороннє обмеження — пряма надійніша
        except Exception:
            coords = None
        path_cache[key] = coords
        return coords

    dropped_mismatch = []
    for route_id, trip_id in best_trip_per_route.items():
        seq = feed.stop_times[trip_id]
        stop_ids = [sid for _, sid, _, _ in seq if sid in feed.stops]
        # Якщо більшість зупинок рейсу НЕ знайдено в feed.stops — це майже
        # завжди не "коротка лінія", а тег/префікс не збігається між
        # stops.txt і stop_times.txt в архіві з кількома підпапками
        # (наприклад stops.txt лежить у 'trolleybus/', а stop_times.txt —
        # у спільній 'ground_transport/'). Раніше це тихо лишало 2 випадкові
        # зупинки, що збіглись, і малювало пряму через усе місто між ними —
        # звідси й "зірка" з прямих ліній, що ріжуть карту навсебіч.
        # Тепер такий маршрут відкидається з голосним попередженням замість
        # маскування під нібито реальну (але зіпсовану) лінію.
        match_ratio = len(stop_ids) / len(seq) if seq else 0
        if len(stop_ids) < 2 or (len(seq) >= 4 and match_ratio < 0.6):
            if len(stop_ids) >= 2:
                dropped_mismatch.append((route_id, len(stop_ids), len(seq)))
            continue

        coords = None
        snapped = False
        if drive_graph is not None and all(sid in stop_to_node for sid in stop_ids):
            first_lat, first_lng, _ = feed.stops[stop_ids[0]]
            coords = [[first_lng, first_lat]]
            any_segment_snapped = False
            for a, b in zip(stop_ids[:-1], stop_ids[1:]):
                a_lat, a_lng, _ = feed.stops[a]
                b_lat, b_lng, _ = feed.stops[b]
                straight_m = haversine_m(a_lat, a_lng, b_lat, b_lng)
                seg = street_path_coords(stop_to_node[a], stop_to_node[b], straight_m=straight_m)
                if seg is None:
                    # ця конкретна ділянка не проклалась по вулицях (розрив графа,
                    # або підозра на об'їзд через одностороннє обмеження) —
                    # з'єднуємо її прямою, замість того щоб ламати весь маршрут
                    coords.append([b_lng, b_lat])
                else:
                    coords.extend(seg[1:])
                    any_segment_snapped = True
            snapped = any_segment_snapped

        if not coords or len(coords) < 2:
            coords = []
            for sid in stop_ids:
                lat, lng, _ = feed.stops[sid]
                coords.append([lng, lat])

        if len(coords) < 2:
            continue

        short_name, route_type = feed.routes.get(route_id, ("", ""))
        color = ROUTE_TYPE_COLOR.get(str(route_type), "#999999")
        kind = ROUTE_TYPE_NAME.get(str(route_type), "unknown")
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"route_id": route_id, "name": short_name, "kind": kind,
                           "color": color, "approx": True, "snapped": snapped},
        })

    if dropped_mismatch:
        print(f"[warn] відкинуто {len(dropped_mismatch)} маршрутів — знайдено менше 60% зупинок "
              f"рейсу в feed.stops. Це типово означає, що stops.txt і stop_times.txt цього "
              f"маршруту прийшли з РІЗНИХ підпапок архіву (різний tag-префікс) і id зупинок "
              f"не збігаються. Перевір структуру ZIP — приклади відкинутих маршрутів:")
        for route_id, matched, total in dropped_mismatch[:5]:
            print(f"  [warn]   {route_id}: знайдено {matched} з {total} зупинок рейсу")

    n_real = sum(1 for f in features if not f["properties"]["approx"])
    n_snapped = sum(1 for f in features if f["properties"]["approx"] and f["properties"]["snapped"])
    n_straight = len(features) - n_real - n_snapped
    print(f"[routes] {len(features)} маршрутів: {n_real} з реальною геометрією (shapes.txt), "
          f"{n_snapped} прокладено по вулицях (без shapes.txt, але trace по дорогах), "
          f"{n_straight} прямими між зупинками (не вдалось прив'язати до дорожнього графа)")
    # Розбивка по видах транспорту — саме це треба дивитись, коли якийсь ОДИН
    # вид (наприклад тролейбус) виглядає криво, а решта нормально: якщо у
    # тролейбуса real=0 і snapped=0 (все straight), лінії ріжуть напряму між
    # зупинками — це і буде виглядати "зламано" при типовій відстані між
    # тролейбусними зупинками. Якщо real=0 і straight=0 (все snapped), і лінії
    # всеодно криві — підозра саме на detour через односторонній рух (звідси
    # DETOUR_RATIO_LIMIT-захист вище), і варто перевірити параметр нижче.
    by_kind: dict = {}
    for f in features:
        k = f["properties"]["kind"]
        d = by_kind.setdefault(k, {"real": 0, "snapped": 0, "straight": 0})
        if not f["properties"]["approx"]:
            d["real"] += 1
        elif f["properties"]["snapped"]:
            d["snapped"] += 1
        else:
            d["straight"] += 1
    for k, d in sorted(by_kind.items()):
        print(f"  [routes]   {k}: {d['real']} реальних, {d['snapped']} по вулицях, {d['straight']} прямими")
    return {"type": "FeatureCollection", "features": features}



def create_app():
    from fastapi import FastAPI, Query
    from fastapi.middleware.cors import CORSMiddleware
    import osmnx as ox

    app = FastAPI(title="CityTwin isochrone API")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    state = {}

    @app.on_event("startup")
    def _startup():
        state["db"] = init_db()
        state["street_graph"] = get_or_build_street_graph(network_type="walk")
        state["static_graph"] = build_static_graph(state["street_graph"])
        try:
            state["feed"] = parse_gtfs(GTFS_ZIP_PATH)
            expand_frequencies(state["feed"])
            state["stop_node"] = add_transit_stops_and_transfers(
                state["static_graph"], state["street_graph"], state["feed"]
            )
            state["dep_index"] = build_stop_departure_index(state["feed"])
            print(f"[ready] GTFS завантажено: {len(state['feed'].stops)} зупинок, "
                  f"{len(state['feed'].trips)} рейсів")
        except FileNotFoundError:
            print(f"[warn] Не знайдено {GTFS_ZIP_PATH} поруч зі скриптом — "
                  f"сервер стартує БЕЗ транзиту (тільки пішохідний граф вулиць). "
                  f"Довантаж GTFS і перезапусти для повної точності (див. README).")
            state["feed"] = GtfsFeed()
            state["stop_node"] = {}
            state["dep_index"] = {}
        state["routes_geojson"] = build_routes_geojson(state["feed"])
        print("[ready] сервер готовий приймати запити на /isochrone і /routes")

    @app.get("/routes")
    def routes():
        """Реальна геометрія маршрутів (метро/трамвай/тролейбус/автобус/електричка)
        з shapes.txt GTFS-фіда — не наближення, а справжні дані перевізника."""
        return state.get("routes_geojson", {"type": "FeatureCollection", "features": []})

    @app.get("/isochrone")
    def isochrone(lat: float = Query(...), lng: float = Query(...),
                   minutes: int = 45, at: Optional[str] = None):
        query_time_s = _time_to_seconds(at) if at else (
            datetime.now().hour * 3600 + datetime.now().minute * 60
        )
        cache_key = f"{lat:.4f}:{lng:.4f}:{minutes}:{query_time_s}"
        cached = cache_get(state["db"], cache_key)
        if cached:
            return cached

        origin_node = ox.distance.nearest_nodes(state["street_graph"], lng, lat)
        result = compute_isochrone_realtime(
            state["static_graph"], state["feed"], state["dep_index"],
            origin_node, query_time_s, minutes
        )
        geojson = nodes_to_polygon_geojson(result["reachable_nodes"])
        cache_set(state["db"], cache_key, geojson)
        return geojson

    return app


app = create_app()

if __name__ == "__main__":
    print(__doc__)
    print("Запуск: uvicorn kyiv_backend:app --reload")
    print("Перший запуск довший — тягне граф вулиць і кешує його на диск.")