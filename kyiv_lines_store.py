"""
kyiv_lines_store.py
====================
Реальне СЕРВЕРНЕ збереження намальованих ліній (трамвай/метро/дорога) —
чого досі не було в проєкті: state.newLines у kyiv-chas.html живе лише
в пам'яті вкладки браузера й зникає при перезавантаженні сторінки, бо
POST /isochrone-bands-with-hypothetical і /accessibility-grid-with-hypothetical
кожного разу рахують "гіпотезу" й нічого не пишуть на диск.

Цей модуль додає до вже запущеного FastAPI-застосунку (create_extended_app()
з kyiv_backend_extensions.py) SQLite-таблицю custom_lines + CRUD:

    POST   /custom-lines           — зберегти нову лінію
    GET    /custom-lines           — список збережених ліній
    DELETE /custom-lines/{id}      — видалити лінію
    PATCH  /custom-lines/{id}      — увімкнути/вимкнути (active) без видалення

Використовує ТОЙ САМИЙ sqlite-файл, що вже й так відкритий бекендом
(kyiv_cache.sqlite, base.DB_PATH) — окреме з'єднання з
check_same_thread=False, як і в base.init_db().

ЯК ПІДКЛЮЧИТИ (2 рядки в kyiv_backend_extensions.py):

    import kyiv_lines_store
    kyiv_lines_store.register(app, state)   # одразу після create_extended_app()'s app = FastAPI(...)

Це просто додає ендпоінти на той самий `app` — нічого в існуючому коді
extensions-файлу міняти не треба.

ЩО ЦЕ ДАЄ, А ЩО НІ:
  - ДАЄ: лінії, які намалював адмін/міськрада, переживають перезапуск
    сервера й перезавантаження сторінки браузера, їх бачать усі клієнти,
    що зайдуть на /custom-lines.
  - НЕ підмінює автоматично /isochrone і /accessibility-grid (базові,
    без "-with-hypothetical") — вони, як і раніше, рахують ТІЛЬКИ реальну
    інфраструктуру. Щоб збережені лінії постійно враховувались навіть
    без явного POST з фронтенду, використовуйте
    /isochrone-bands-with-hypothetical?include_saved=true (нижче) — тоді
    бекенд домішує збережені активні лінії до тих, що прийшли в тілі
    запиту, перед вбудовуванням у граф.
"""

import json
import sqlite3
import time
from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel


class SavedLineIn(BaseModel):
    type: str            # "tram" | "metro" | "road"
    points: List[List[float]]   # [[lat, lng], ...]
    name: Optional[str] = None
    author: Optional[str] = None


class SavedLineOut(SavedLineIn):
    id: int
    created_at: float
    active: bool


def _init_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            points_json TEXT NOT NULL,
            name TEXT,
            author TEXT,
            created_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()


def _row_to_out(row) -> dict:
    return {
        "id": row[0],
        "type": row[1],
        "points": json.loads(row[2]),
        "name": row[3],
        "author": row[4],
        "created_at": row[5],
        "active": bool(row[6]),
    }


def register(app, state: dict):
    """Додає /custom-lines* ендпоінти до вже створеного FastAPI app.
    state — той самий словник стану, що й у create_extended_app()
    (потрібен лише state["db"], уже відкритий base.init_db())."""

    conn: sqlite3.Connection = state["db"]
    _init_table(conn)

    @app.post("/custom-lines", response_model=SavedLineOut)
    def create_custom_line(line: SavedLineIn):
        if line.type not in ("tram", "metro", "road"):
            raise HTTPException(400, "type має бути tram/metro/road")
        if len(line.points) < 2:
            raise HTTPException(400, "лінія потребує щонайменше 2 точок")
        now = time.time()
        cur = conn.execute(
            "INSERT INTO custom_lines (type, points_json, name, author, created_at, active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (line.type, json.dumps(line.points), line.name, line.author, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, type, points_json, name, author, created_at, active "
            "FROM custom_lines WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_out(row)

    @app.get("/custom-lines", response_model=List[SavedLineOut])
    def list_custom_lines(active_only: bool = True):
        q = "SELECT id, type, points_json, name, author, created_at, active FROM custom_lines"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY created_at DESC"
        rows = conn.execute(q).fetchall()
        return [_row_to_out(r) for r in rows]

    @app.delete("/custom-lines/{line_id}")
    def delete_custom_line(line_id: int):
        cur = conn.execute("DELETE FROM custom_lines WHERE id = ?", (line_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "лінію не знайдено")
        return {"deleted": line_id}

    @app.patch("/custom-lines/{line_id}", response_model=SavedLineOut)
    def toggle_custom_line(line_id: int, active: bool):
        conn.execute("UPDATE custom_lines SET active = ? WHERE id = ?", (1 if active else 0, line_id))
        conn.commit()
        row = conn.execute(
            "SELECT id, type, points_json, name, author, created_at, active "
            "FROM custom_lines WHERE id = ?", (line_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "лінію не знайдено")
        return _row_to_out(row)

    def load_active_as_hypothetical():
        """Повертає активні збережені лінії у форматі HypotheticalLine
        (type/points) — щоб їх можна було домішати до req.lines перед
        add_hypothetical_lines_to_graph()."""
        rows = conn.execute(
            "SELECT type, points_json FROM custom_lines WHERE active = 1 AND type != 'road'"
        ).fetchall()
        return [{"type": r[0], "points": json.loads(r[1])} for r in rows]

    # Віддаємо назовні — extensions-файл підключить це в
    # isochrone_bands_with_hypothetical / accessibility_grid_with_hypothetical
    # через include_saved=true (див. приклад підключення нижче).
    state["load_saved_lines"] = load_active_as_hypothetical
