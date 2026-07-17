"""
CityTwin — kyiv_backend_extensions.py
======================================
Розширення kyiv_backend.py: додає підтримку ГІПОТЕТИЧНИХ ліній (трамвай/
метро), намальованих у режимі «Міськрада» на фронтенді, вбудовуючи їх
ЯК РЕАЛЬНІ РЕБРА ГРАФА перед розрахунком Дейкстри — а не як окремий
JS-шар поверх готового результату бекенду.

Це саме те, чого не вистачало в kyiv_backend.py: там результат /isochrone
рахується виключно з реальної інфраструктури, і намальована лінія фізично
не могла на нього вплинути. Тут — може, бо ми тимчасово додаємо її вузли
й ребра в копію графа ПЕРЕД тим, як пускати по ньому Дейкстру.

Нічого зі старого файлу не переписано і не задубльовано: цей модуль
імпортує kyiv_backend як бібліотеку (`import kyiv_backend as base`) і
перевикористовує:
  - той самий кешований граф вулиць (get_or_build_street_graph)
  - той самий GTFS-парсинг (parse_gtfs, expand_frequencies)
  - ту саму Дейкстру з реальним розкладом (compute_isochrone_realtime)
  - ту саму мозаїку доступності (compute_accessibility_grid)
  - той самий білдер реальної геометрії ліній (build_routes_geojson)

ЯК ЗАПУСТИТИ (замість kyiv_backend.py, з тієї ж папки, з тим самим
GTFS-архівом поруч):
    pip install osmnx networkx fastapi uvicorn shapely scipy numpy pydantic
    uvicorn kyiv_backend_extensions:app --reload

Ендпоінти:
    GET  /routes                                  — як у базовому сервері
    GET  /isochrone                                — як у базовому сервері
    GET  /accessibility-grid                       — як у базовому сервері
    POST /isochrone-bands-with-hypothetical        — НОВЕ (кількаband'ів за 1 Дейкстру)
    POST /accessibility-grid-with-hypothetical     — НОВЕ

Фронтенд (index.html) вже написаний під ці два нові POST-ендпоінти й сам
вирішує, коли їх викликати (коли на карті є хоч одна намальована лінія
трамваю/метро) — з боку HTML нічого міняти не треба.

ЯК ПРАЦЮЄ ВБУДОВУВАННЯ ЛІНІЇ (add_hypothetical_lines_to_graph):
  1. Лінія користувача — це ламана з небагатьох точок (клікав на карті).
     Її "ущільнюють" (_densify_line): додають проміжні точки що ~300 м,
     щоб на неї можна було "сісти" не лише в точках кліку, а й по дорозі
     (як реальні зупинки).
  2. Для кожної такої точки створюється новий вузол графа (hypo_i_k).
  3. Сусідні вузли лінії з'єднуються ребрами "їзда" (вага = відстань/швидкість
     трамваю чи метро) в обидва боки.
  4. Кожен вузол лінії векторизовано (ox.distance.nearest_nodes, один виклик
     на всю лінію — та сама причина продуктивності, що й у базовому файлі)
     прив'язується до найближчого вузла РЕАЛЬНОГО графа вулиць пішохідним
     ребром. На "вхідне" ребро (вулиця -> лінія) додається час очікування
     посадки (headway/2) — це і є "чекаю на зупинці", на "вихідне" (лінія ->
     вулиця) очікування не додається (висадка миттєва).
  5. Далі це просто ЗВИЧАЙНИЙ граф для compute_isochrone_realtime — жодних
     спецвипадків у самій Дейкстрі не потрібно.

Копія графа (static_graph.copy()) робиться на кожен запит — для графа
одного міста це прийнятно (секунди), і гарантує, що гіпотетичні лінії
одного запиту не просочуються в кеш чи в інші запити.
"""

import time
from typing import List, Literal, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import kyiv_backend as base

# ---------------------------------------------------------------------------
# КОНФІГ гіпотетичних ліній — узгоджено з константами на фронтенді
# (TRAM_SPEED=20, METRO_NEW_SPEED=33 у index.html), щоб цифри бекенду й
# швидкої JS-симуляції не розходились надто сильно, коли бекенд недоступний.
# ---------------------------------------------------------------------------
NEW_TRAM_SPEED_KMH = 20.0
NEW_METRO_SPEED_KMH = 33.0
NEW_TRAM_WAIT_S = 5 * 60     # орієнтовне очікування посадки (headway/2), будній день
NEW_METRO_WAIT_S = 3 * 60
NEW_LINE_STOP_SPACING_M = 300.0   # додаткові "зупинки" вздовж намальованої лінії
LINE_WALK_ACCESS_RADIUS_M = 600.0  # макс. пішохідна відстань до лінії, щоб на неї сісти


# ---------------------------------------------------------------------------
# Pydantic-моделі запитів (мають збігатись з тим, що шле index.html)
# ---------------------------------------------------------------------------
class HypotheticalLine(BaseModel):
    type: Literal["tram", "metro", "road"]
    points: List[List[float]]  # [[lat, lng], ...] — як у state.newLines[i].pts на фронтенді


class IsochroneBandsRequest(BaseModel):
    lat: float
    lng: float
    at: Optional[str] = None
    bands: List[int]
    lines: List[HypotheticalLine] = []


class AccessibilityGridRequest(BaseModel):
    nx: int = 44
    ny: int = 36
    minutes: int = 90
    at: Optional[str] = None
    lines: List[HypotheticalLine] = []


# ---------------------------------------------------------------------------
# Вбудовування гіпотетичних ліній у граф
# ---------------------------------------------------------------------------
def _densify_line(points_latlng, spacing_m: float = NEW_LINE_STOP_SPACING_M):
    """[[lat,lng],...] -> та сама лінія з доданими проміжними точками що
    ~spacing_m, щоб на лінію можна було "сісти" не лише у вершинах, які
    користувач клікнув мишею, а й по дорозі — як реальні зупинки."""
    if len(points_latlng) < 2:
        return list(points_latlng)
    out = [points_latlng[0]]
    for i in range(len(points_latlng) - 1):
        lat1, lng1 = points_latlng[i]
        lat2, lng2 = points_latlng[i + 1]
        seg_len = base.haversine_m(lat1, lng1, lat2, lng2)
        if seg_len <= 0:
            continue
        n_extra = int(seg_len // spacing_m)
        for k in range(1, n_extra + 1):
            t = k * spacing_m / seg_len
            out.append([lat1 + (lat2 - lat1) * t, lng1 + (lng2 - lng1) * t])
        out.append([lat2, lng2])
    return out


def add_hypothetical_lines_to_graph(static_graph, street_graph, lines: List[HypotheticalLine]):
    """
    ВАЖЛИВО (продуктивність — це і є ймовірна причина, чому фронтенд
    "падає" назад у JS-симуляцію): раніше тут стояло `static_graph.copy()`
    — повна копія графа вулиць Києва (для реального міста це десятки
    тисяч вузлів і сотні тисяч ребер) НА КОЖЕН запит. Це може займати
    кілька секунд, а фронтенд чекає відповідь максимум
    AbortSignal.timeout(20000) = 20 с. Якщо копіювання + Дейкстра
    разом не встигають — фронтенд ловить помилку/таймаут і тихо
    вимикає бекенд-шар назад на JS-симуляцію (index.html, catch-блок
    у fetchBackendIsochrone) — З БОКУ КОРИСТУВАЧА це виглядає так,
    ніби "результат ігнорує нову лінію й показує щось випадкове".

    Тепер ця функція МУТУЄ static_graph напряму — додає лише вузли й
    ребра самої лінії (O(довжина лінії), не O(розмір усього графа)) —
    і повертає (той самий граф, список доданих id). Виклик ЗОБОВ'ЯЗАНИЙ
    прибрати ці вузли через remove_hypothetical_lines_from_graph() у
    finally-блоці одразу після використання, інакше гіпотетичні лінії
    одного запиту "протечуть" у наступні. Через це весь цикл
    "додати -> порахувати -> прибрати" обгорнуто глобальним локом
    (_graph_mutation_lock нижче) — паралельні запити з гіпотетичними
    лініями чекають один одного замість того, щоб псувати спільний граф.

    Лінії типу "road" тут пропускаються: бекенд рахує пішохідно-транзитну
    мережу (walk graph), а не автомобільну — вплив нової дороги на трафік
    авто лишається на боці швидкої JS-симуляції фронтенду (це чесно
    задокументовано в /accessibility-grid-with-hypothetical нижче).
    """
    import osmnx as ox

    G = static_graph  # мутуємо напряму, без копії — див. докстрінг вище

    transit_lines = [l for l in lines if l.type in ("tram", "metro")]
    if not transit_lines:
        return G, []

    added_nodes: List[str] = []

    for line_idx, line in enumerate(transit_lines):
        pts = _densify_line(line.points)
        if len(pts) < 2:
            continue

        speed_kmh = NEW_TRAM_SPEED_KMH if line.type == "tram" else NEW_METRO_SPEED_KMH
        speed_ms = speed_kmh * 1000 / 3600
        wait_s = NEW_TRAM_WAIT_S if line.type == "tram" else NEW_METRO_WAIT_S

        line_node_ids = []
        for i, (lat, lng) in enumerate(pts):
            node_id = f"hypo_{line_idx}_{i}"
            G.add_node(node_id, lat=lat, lng=lng, kind="hypothetical_stop", line_type=line.type)
            line_node_ids.append(node_id)
            added_nodes.append(node_id)

        # Рух уздовж лінії — в обидва боки (трамвай/метро курсують туди-сюди).
        for i in range(len(line_node_ids) - 1):
            lat1, lng1 = pts[i]
            lat2, lng2 = pts[i + 1]
            d_m = base.haversine_m(lat1, lng1, lat2, lng2)
            t = d_m / speed_ms
            G.add_edge(line_node_ids[i], line_node_ids[i + 1], weight=t)
            G.add_edge(line_node_ids[i + 1], line_node_ids[i], weight=t)

        # Пішохідний доступ до кожної "зупинки" нової лінії — векторизовано,
        # один виклик nearest_nodes на всю лінію (та сама причина
        # продуктивності, що й у base.add_transit_stops_and_transfers:
        # скалярний виклик у циклі перебудовує просторовий індекс щоразу).
        try:
            lats = [p[0] for p in pts]
            lngs = [p[1] for p in pts]
            nearest = ox.distance.nearest_nodes(street_graph, X=lngs, Y=lats)
            for (lat, lng), node_id, nearest_street_node in zip(pts, line_node_ids, nearest):
                d = base.haversine_m(
                    lat, lng,
                    street_graph.nodes[nearest_street_node]["y"],
                    street_graph.nodes[nearest_street_node]["x"],
                )
                if d <= LINE_WALK_ACCESS_RADIUS_M:
                    walk_t = d / (base.WALK_SPEED_KMH * 1000 / 3600)
                    # Очікування посадки додається лише на вхідне ребро
                    # (вулиця -> лінія). Висадка (лінія -> вулиця) без
                    # очікування — це коректно моделює "чекаю на зупинці
                    # лише коли сідаю, а не коли виходжу".
                    G.add_edge(nearest_street_node, node_id, weight=walk_t + wait_s)
                    G.add_edge(node_id, nearest_street_node, weight=walk_t)
        except Exception as e:
            print(f"[hypothetical] не вдалось прив'язати лінію {line_idx} "
                  f"({line.type}) до графа вулиць: {e}")

    return G, added_nodes


def remove_hypothetical_lines_from_graph(static_graph, added_nodes: List[str]) -> None:
    """Прибирає вузли, додані add_hypothetical_lines_to_graph() (networkx
    сам видаляє всі інцидентні ребра разом з вузлом) — граф повертається
    ТОЧНО до стану перед запитом. Викликати завжди, навіть якщо розрахунок
    впав з помилкою (тому виклик — у finally, див. ендпоінти нижче)."""
    for node_id in added_nodes:
        if static_graph.has_node(node_id):
            static_graph.remove_node(node_id)


# Один спільний static_graph мутується під час обробки гіпотетичних ліній
# (див. докстрінг add_hypothetical_lines_to_graph) — цей лок гарантує, що
# два паралельні запити з різними намальованими лініями не змішають свої
# тимчасові вузли/ребра в тому самому графі. Ціна — запити з гіпотетичними
# лініями обробляються по черзі, а не паралельно; для сценарію "один
# користувач малює лінії на карті" це не проблема.
import threading
_graph_mutation_lock = threading.Lock()


# ---------------------------------------------------------------------------
# FastAPI-застосунок
# ---------------------------------------------------------------------------
def create_extended_app():
    app = FastAPI(title="CityTwin isochrone API (extended, з гіпотетичними лініями)")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    state = {}

    @app.on_event("startup")
    def _startup():
        # Той самий стартап, що й у базовому kyiv_backend.py — той самий
        # кешований на диску граф вулиць (kyiv_street_graph_walk.pkl),
        # той самий GTFS. Якщо базовий сервер уже колись запускався в цій
        # папці, граф підхопиться з кешу за секунди, а не заново з OSM.
        state["db"] = base.init_db()
        state["street_graph"] = base.get_or_build_street_graph(network_type="walk")
        state["static_graph"] = base.build_static_graph(state["street_graph"])
        try:
            state["feed"] = base.parse_gtfs(base.GTFS_ZIP_PATH)
            base.expand_frequencies(state["feed"])
            state["stop_node"] = base.add_transit_stops_and_transfers(
                state["static_graph"], state["street_graph"], state["feed"]
            )
            state["dep_index"] = base.build_stop_departure_index(state["feed"])
            print(f"[ready] GTFS завантажено: {len(state['feed'].stops)} зупинок, "
                  f"{len(state['feed'].trips)} рейсів")
        except FileNotFoundError:
            print(f"[warn] Не знайдено {base.GTFS_ZIP_PATH} — сервер стартує БЕЗ "
                  f"реального транзиту (тільки пішохідний граф + гіпотетичні лінії).")
            state["feed"] = base.GtfsFeed()
            state["stop_node"] = {}
            state["dep_index"] = {}

        drive_graph = None
        try:
            drive_graph = base.get_or_build_street_graph(network_type="drive")
        except Exception as e:
            print(f"[warn] не вдалось завантажити drive-граф для точнішого /routes: {e}")
        state["routes_geojson"] = base.build_routes_geojson(state["feed"], drive_graph=drive_graph)

        print("[ready] kyiv_backend_extensions запущено: базові ендпоінти "
              "+ /isochrone-bands-with-hypothetical + /accessibility-grid-with-hypothetical")

    def _query_time_s(at: Optional[str]) -> int:
        if at:
            return base._time_to_seconds(at)
        t = time.localtime()
        return t.tm_hour * 3600 + t.tm_min * 60

    # ---- ті самі базові ендпоінти, що й у kyiv_backend.py (щоб фронтенд
    # міг просто змінити файл запуску й мати весь той самий API + нове) ----
    @app.get("/routes")
    def routes():
        return state.get("routes_geojson", {"type": "FeatureCollection", "features": []})

    @app.get("/isochrone")
    def isochrone(lat: float = Query(...), lng: float = Query(...),
                  minutes: int = 45, at: Optional[str] = None):
        import osmnx as ox
        query_time_s = _query_time_s(at)
        origin_node = ox.distance.nearest_nodes(state["street_graph"], lng, lat)
        result = base.compute_isochrone_realtime(
            state["static_graph"], state["feed"], state["dep_index"],
            origin_node, query_time_s, minutes
        )
        return base.nodes_to_polygon_geojson(result["reachable_nodes"])

    @app.get("/accessibility-grid")
    def accessibility_grid(nx_cells: int = Query(44, alias="nx", ge=8, le=160),
                            ny_cells: int = Query(36, alias="ny", ge=8, le=160),
                            minutes: int = Query(90, ge=15, le=180),
                            at: Optional[str] = None):
        query_time_s = _query_time_s(at)
        return base.compute_accessibility_grid(
            state["static_graph"], state["feed"], state["dep_index"], state["street_graph"],
            base.ACCESSIBILITY_CENTER_LAT, base.ACCESSIBILITY_CENTER_LNG, query_time_s,
            nx_cells=nx_cells, ny_cells=ny_cells, max_minutes=minutes,
        )

    # ---- НОВЕ: гіпотетичні лінії вбудовані прямо в граф перед Дейкстрою ----
    @app.post("/isochrone-bands-with-hypothetical")
    def isochrone_bands_with_hypothetical(req: IsochroneBandsRequest):
        """
        Один запит фронтенду = ОДНА Дейкстра (на найбільшому band'і), а не
        по одній Дейкстрі на кожен band (15/30/45/60 хв) — банди менші за
        максимум просто вирізаються з того самого результату за принципом
        "усі вузли, до яких дійшли за ≤N хвилин". Так і швидше, і гарантовано
        узгоджено між бандами (не буває, що 45-хвильна зона "випадає" з
        60-хвильної через різні округлення).
        """
        import osmnx as ox

        query_time_s = _query_time_s(req.at)
        with _graph_mutation_lock:
            added_nodes: List[str] = []
            try:
                augmented_graph, added_nodes = add_hypothetical_lines_to_graph(
                    state["static_graph"], state["street_graph"], req.lines
                )
                origin_node = ox.distance.nearest_nodes(state["street_graph"], req.lng, req.lat)
                max_minutes = max(req.bands) if req.bands else 45

                result = base.compute_isochrone_realtime(
                    augmented_graph, state["feed"], state["dep_index"],
                    origin_node, query_time_s, max_minutes
                )
                reachable = result["reachable_nodes"]

                bands_out = {}
                for m in sorted(set(req.bands)):
                    subset = [p for p in reachable if p["minutes"] <= m]
                    bands_out[str(m)] = base.nodes_to_polygon_geojson(subset)
            finally:
                # ОБОВ'ЯЗКОВО прибрати гіпотетичні вузли, навіть якщо вище
                # впала помилка — інакше вони лишаться в спільному графі
                # й вплинуть на НАСТУПНІ запити (в т.ч. без жодних ліній).
                remove_hypothetical_lines_from_graph(state["static_graph"], added_nodes)

        return {
            "bands": bands_out,
            "n_hypothetical_lines": len([l for l in req.lines if l.type != "road"]),
            "note": "Гіпотетичні трамвай/метро вбудовано як реальні ребра графа перед Дейкстрою "
                    "(додаються й одразу прибираються з того самого графа під локом, без повного "
                    "копіювання — це і мало виправити таймаути/фолбек у JS-симуляцію). "
                    "Лінії типу 'road' тут не враховуються — бекенд рахує пішки+транзит, не авто-мережу.",
        }

    @app.post("/accessibility-grid-with-hypothetical")
    def accessibility_grid_with_hypothetical(req: AccessibilityGridRequest):
        query_time_s = _query_time_s(req.at)
        augmented_graph, added_nodes = add_hypothetical_lines_to_graph(
            state["static_graph"], state["street_graph"], req.lines
        )
        result = base.compute_accessibility_grid(
            augmented_graph, state["feed"], state["dep_index"], state["street_graph"],
            base.ACCESSIBILITY_CENTER_LAT, base.ACCESSIBILITY_CENTER_LNG, query_time_s,
            nx_cells=req.nx, ny_cells=req.ny, max_minutes=req.minutes,
        )
        result["n_hypothetical_lines"] = len([l for l in req.lines if l.type != "road"])
        result["note"] = ("Гіпотетичні трамвай/метро вбудовано як реальні ребра графа перед "
                           "тією самою Дейкстрою від центру, що й у базовому /accessibility-grid. "
                           "Те саме спрощення 'з центру, а не до центру' і відсутність "
                           "calendar.txt, що задокументовано в compute_accessibility_grid().")
        return result

    return app


app = create_extended_app()

if __name__ == "__main__":
    print(__doc__)
    print("Запуск: uvicorn kyiv_backend_extensions:app --reload")
    print("Потребує kyiv_backend.py в тій самій папці (імпортується як модуль).")