import sqlite3, base64, re, os
from datetime import date, datetime, timedelta
from flask import Flask, jsonify, request, Response, send_from_directory
from pdf_parser import extract_meals_and_images_from_pdf, scale_ingredients_text
import pdf_parser as _p
import gc

DB = "przepisy.db"
SLOTS = {"Śniadanie": "6:00-9:00", "Drugie śniadanie": "9:00-11:00", "Lunch": "11:00-14:00",
         "Obiad": "14:00-17:00", "Kolacja": "18:00-21:00", "Dodatkowy": ""}
ORDER = list(SLOTS)
DNI = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]
GOAL = dict(kcal=1900, b=110, t=65, w=210)
app = Flask(__name__, static_folder="static")

class DictRow(dict):
    """Obiekt wiersza obsługujący zarówno dostęp r['kolumna'] jak i r[0]."""
    def __init__(self, cols, vals):
        super().__init__(zip(cols, vals))
        self._vals = tuple(vals)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)

class DictCursor:
    """Kursor dopasowujący wyniki Turso do składni SQLite."""
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, sql, params=()):
        self.cursor.execute(sql, params)
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, (dict, sqlite3.Row)):
            return row
        cols = [d[0] for d in self.cursor.description]
        return DictRow(cols, row)

    def fetchall(self):
        rows = self.cursor.fetchall()
        if not rows:
            return []
        if isinstance(rows[0], (dict, sqlite3.Row)):
            return rows
        cols = [d[0] for d in self.cursor.description]
        return [DictRow(cols, r) for r in rows]

    @property
    def lastrowid(self):
        return getattr(self.cursor, "lastrowid", None)

class DBWrapper:
    def __init__(self):
        db_url = os.environ.get("TURSO_DATABASE_URL")
        db_token = os.environ.get("TURSO_AUTH_TOKEN")
        if db_url and db_token:
            import libsql_experimental as libsql
            self.conn = libsql.connect(database=db_url, auth_token=db_token)
            self.is_turso = True
        else:
            self.conn = sqlite3.connect(DB)
            self.conn.row_factory = sqlite3.Row
            self.is_turso = False

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        if self.is_turso:
            return DictCursor(cur)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()

def db():
    return DBWrapper()

def norm(t): return re.sub(r"\s+", " ", (t or "").strip().lower())

def pack_img(u):
    if not u:
        return None
    try:
        import io, gc
        from PIL import Image

        match = re.match(r"data:image/\w+;base64,(.*)", u, re.S)
        if not match:
            return u
            
        img_bytes = base64.b64decode(match.group(1))
        im = Image.open(io.BytesIO(img_bytes))
        im.thumbnail((450, 450), Image.Resampling.LANCZOS)
        
        buf = io.BytesIO()
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGBA")
            im.save(buf, "WEBP", quality=75)
        else:
            im = im.convert("RGB")
            im.save(buf, "WEBP", quality=75, optimize=True)

        im.close()
        gc.collect()

        return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
    
def add_slot(old, nr):
    parts = {x for x in (old or "").split(",") if x}
    for n in ([nr] if isinstance(nr, int) else nr):
        if 1 <= (n or 0) <= 5: parts.add(str(n))
    return "," + ",".join(sorted(parts)) + "," if parts else ""

PREF = re.compile(r"^\s*Posiłek\s*(\d+)\s*(/\s*(dowolna pora dnia|\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}))?\s*", re.I)
def clean_title(t):
    m = PREF.match(t or "")
    return ((PREF.sub("", t).strip() or t), int(m.group(1)) if m else 0)

def slots_of(orig, nr):
    return [1, 2, 3, 4, 5] if "dowolna" in (orig or "").lower() else [nr]

def migrate():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS przepisy(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tytul TEXT, skladniki TEXT, przygotowanie TEXT, 
            kcal REAL, bialko REAL, wegle REAL, tluszcze REAL, image_url TEXT, zrodlo TEXT, 
            sloty TEXT DEFAULT '', img_ok INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS dzien_plan(id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT,
            slot TEXT, przepis_id INTEGER, stan TEXT DEFAULT 'plan', porcje REAL DEFAULT 1.0,
            FOREIGN KEY(przepis_id) REFERENCES przepisy(id) ON DELETE CASCADE)""")
        c.execute("CREATE TABLE IF NOT EXISTS zakupy(id INTEGER PRIMARY KEY AUTOINCREMENT, tekst TEXT, kupione INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS korekta(data TEXT PRIMARY KEY, kcal INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS ustawienia(klucz TEXT PRIMARY KEY, wartosc TEXT)")

        cols = [r[1] for r in c.execute("PRAGMA table_info(przepisy)").fetchall()]

        for n, t in (("sloty", "TEXT DEFAULT ''"), ("img_ok", "INTEGER DEFAULT 0")):
            if n not in cols:
                c.execute(f"ALTER TABLE przepisy ADD COLUMN {n} {t}")

        for r in c.execute("SELECT id, tytul, sloty FROM przepisy").fetchall():
            t, nr = clean_title(r["tytul"])
            if t != r["tytul"]:
                c.execute("UPDATE przepisy SET tytul=?, sloty=? WHERE id=?", (t, add_slot(r["sloty"], slots_of(r["tytul"], nr)), r["id"]))

        keep, sl = {}, {}
        for r in c.execute("SELECT id, tytul, sloty FROM przepisy ORDER BY id").fetchall():
            k = norm(r["tytul"])
            if k in keep:
                u = {x for x in ((sl[k] or "") + "," + (r["sloty"] or "")).split(",") if x}
                sl[k] = "," + ",".join(sorted(u)) + "," if u else ""
                c.execute("UPDATE przepisy SET sloty=? WHERE id=?", (sl[k], keep[k]))
                for tb in ("planer", "dziennik", "dzien_plan"):
                    try:
                        c.execute(f"UPDATE {tb} SET przepis_id=? WHERE przepis_id=?", (keep[k], r["id"]))
                    except:
                        pass
                c.execute("DELETE FROM przepisy WHERE id=?", (r["id"],))
            else:
                keep[k], sl[k] = r["id"], r["sloty"]

        for r in c.execute("SELECT id, image_url FROM przepisy WHERE img_ok=0 AND image_url!=''").fetchall():
            n = pack_img(r["image_url"])
            if n:
                c.execute("UPDATE przepisy SET image_url=?, img_ok=1 WHERE id=?", (n, r["id"]))

migrate()

def sync_local_to_turso():
    if not os.path.exists("przepisy.db") or "TURSO_DATABASE_URL" not in os.environ:
        return
    try:
        local_conn = sqlite3.connect("przepisy.db")
        with db() as turso:
            count = turso.execute("SELECT COUNT(*) FROM przepisy").fetchone()
            if count and count[0] > 0:
                return
            
            tables = [r[0] for r in local_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
            for table in tables:
                cols = [c[1] for c in local_conn.execute(f"PRAGMA table_info({table})").fetchall()]
                placeholders = ", ".join(["?"] * len(cols))
                col_names = ", ".join(cols)
                rows = local_conn.execute(f"SELECT * FROM {table}").fetchall()
                for row in rows:
                    turso.execute(f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})", row)
    except Exception as e:
        print(f"Błąd auto-migracji: {e}")

sync_local_to_turso()

def seed(c, d):
    try:
        if c.execute("SELECT 1 FROM dzien_plan WHERE data=?", (d,)).fetchone(): return
        wd = DNI[datetime.strptime(d, "%Y-%m-%d").weekday()]
        for r in c.execute("SELECT posilek, przepis_id FROM planer WHERE dzien=?", (wd,)).fetchall():
            c.execute("INSERT INTO dzien_plan(data,slot,przepis_id) VALUES(?,?,?)", (d, r["posilek"], r["przepis_id"]))
    except: pass

def meals(c, d):
    seed(c, d)
    rows = c.execute("""SELECT m.id, m.slot, m.stan, m.porcje, p.id rid, p.tytul, p.kcal, p.bialko, p.wegle, p.tluszcze
        FROM dzien_plan m JOIN przepisy p ON p.id=m.przepis_id WHERE m.data=?""", (d,)).fetchall()
    rows = sorted(rows, key=lambda r: (ORDER.index(r["slot"]) if r["slot"] in ORDER else 99, r["id"]))
    return [dict(id=r["id"], porcje=r["porcje"], slot=r["slot"], time=SLOTS.get(r["slot"], ""), stan=r["stan"], rid=r["rid"], tytul=r["tytul"],
                 kcal=round(r["kcal"] * r["porcje"]), b=round(r["bialko"] * r["porcje"]),
                 w=round(r["wegle"] * r["porcje"]), t=round(r["tluszcze"] * r["porcje"])) for r in rows]

def totals(ms, only=None):
    s = dict(kcal=0, b=0, w=0, t=0)
    for m in ms:
        if only is None or m["stan"] == only:
            for k in s: s[k] += m[k]
    return s

@app.get("/")
def index(): return send_from_directory("static", "index.html")

def goal(c):
    g = dict(GOAL); v = c.execute("SELECT wartosc FROM ustawienia WHERE klucz='cel_kcal'").fetchone()
    if v: g["kcal"] = int(v["wartosc"])
    return g

def kor(c, d):
    r = c.execute("SELECT kcal FROM korekta WHERE data=?", (d,)).fetchone(); return r["kcal"] if r else 0

@app.get("/api/day/<d>")
def day(d):
    with db() as c:
        ms = meals(c, d); g = goal(c); k = kor(c, d)
        shop = c.execute("SELECT COUNT(*) FROM zakupy WHERE kupione=0").fetchone()[0]
    e = totals(ms, "zjedzony"); e["kcal"] += k
    return jsonify(meals=ms, eaten=e, kor=k, planned=totals([m for m in ms if m["stan"] != "pominiety"]), goal=g, shop=shop)

@app.get("/api/week/<d>")
def week(d):
    mon = datetime.strptime(d, "%Y-%m-%d").date(); mon -= timedelta(days=mon.weekday())
    out = []
    with db() as c:
        g = goal(c)["kcal"]
        for i in range(7):
            x = (mon + timedelta(days=i)).isoformat(); ms = meals(c, x)
            out.append(dict(date=x, progress=max(0, min(1, (totals(ms, "zjedzony")["kcal"] + kor(c, x)) / g))))
    return jsonify(out)

@app.put("/api/day/<d>/kcal")
def set_kcal(d):
    v = request.json.get("kcal")
    with db() as c:
        if v is None: c.execute("DELETE FROM korekta WHERE data=?", (d,))
        else: c.execute("INSERT OR REPLACE INTO korekta(data,kcal) VALUES(?,?)", (d, int(v) - totals(meals(c, d), "zjedzony")["kcal"]))
    return jsonify(ok=True)

@app.put("/api/goal")
def set_goal():
    with db() as c: c.execute("INSERT OR REPLACE INTO ustawienia(klucz,wartosc) VALUES('cel_kcal',?)", (str(int(request.json["kcal"])),))
    return jsonify(ok=True)

@app.post("/api/day/<d>/meal")
def add_meal(d):
    j = request.json
    with db() as c:
        c.execute("INSERT INTO dzien_plan(data,slot,przepis_id) VALUES(?,?,?)", (d, j.get("slot", "Dodatkowy"), j["rid"]))
    return jsonify(ok=True)

@app.patch("/api/meal/<int:i>")
def patch_meal(i):
    j = request.json
    with db() as c:
        old = c.execute("SELECT data, przepis_id FROM dzien_plan WHERE id=?", (i,)).fetchone()
        if "stan" in j: c.execute("UPDATE dzien_plan SET stan=? WHERE id=?", (j["stan"], i))
        if "rid" in j: c.execute("UPDATE dzien_plan SET przepis_id=?, stan='plan', porcje=1 WHERE id=?", (j["rid"], i))
        if "porcje" in j: c.execute("UPDATE dzien_plan SET porcje=? WHERE id=?", (max(0.5, float(j["porcje"])), i))
        try:
            c.execute("DELETE FROM dziennik WHERE dzien_data=? AND przepis_id=?", (old["data"], old["przepis_id"]))
            n = c.execute("SELECT data, przepis_id, porcje, stan FROM dzien_plan WHERE id=?", (i,)).fetchone()
            if n["stan"] == "zjedzony":
                c.execute("INSERT INTO dziennik(dzien_data,przepis_id,porcje) VALUES(?,?,?)", (n["data"], n["przepis_id"], n["porcje"]))
        except: pass
    return jsonify(ok=True)

@app.delete("/api/meal/<int:i>")
def del_meal(i):
    with db() as c:
        r = c.execute("SELECT data, przepis_id FROM dzien_plan WHERE id=?", (i,)).fetchone()
        try:
            if r: c.execute("DELETE FROM dziennik WHERE dzien_data=? AND przepis_id=?", (r["data"], r["przepis_id"]))
        except: pass
        c.execute("DELETE FROM dzien_plan WHERE id=?", (i,))
    return jsonify(ok=True)

@app.get("/api/recipes")
def recipes():
    qs = request.args.get("q", "").lower(); mx = int(request.args.get("max", 5000)); slot = int(request.args.get("slot", 0))
    with db() as c:
        rows = c.execute("SELECT id,tytul,skladniki,kcal,bialko,wegle,tluszcze,sloty FROM przepisy WHERE kcal<=? ORDER BY tytul", (mx,)).fetchall()
        n_all = c.execute("SELECT COUNT(*) FROM przepisy").fetchone()[0]
        n_as = c.execute("SELECT SUM(sloty!='') FROM przepisy").fetchone()[0] or 0
        have = bool(n_all) and n_as >= 0.8 * n_all
    if slot and have: rows = [r for r in rows if f",{slot}," in (r["sloty"] or "")]
    return jsonify(assigned=have, items=[dict(id=r["id"], tytul=r["tytul"], kcal=r["kcal"], b=round(r["bialko"]), w=round(r["wegle"]), t=round(r["tluszcze"]))
                    for r in rows if qs in r["tytul"].lower() or qs in (r["skladniki"] or "").lower()])

def ing_lines(text, f=1.0):
    out = []
    for l in (scale_ingredients_text(text, f) or "").split("\n"):
        if l.startswith("(") and out: out[-1] += " " + l
        elif l.strip(): out.append(l.strip())
    return out

@app.get("/api/recipe/<int:i>")
def recipe(i):
    f = float(request.args.get("porcje", 1))
    with db() as c: r = c.execute("SELECT * FROM przepisy WHERE id=?", (i,)).fetchone()
    if not r: return jsonify(error="nie ma"), 404
    steps = [re.sub(r"^\d+\.\s*", "", x) for x in (r["przygotowanie"] or "").split("\n") if x.strip()]
    return jsonify(id=i, tytul=r["tytul"], kcal=round(r["kcal"] * f), b=round(r["bialko"] * f, 1), w=round(r["wegle"] * f, 1),
                   t=round(r["tluszcze"] * f, 1), skladniki=ing_lines(r["skladniki"], f), kroki=steps)

@app.put("/api/recipe/<int:i>")
def recipe_put(i):
    j = request.json
    if not j["tytul"].strip(): return jsonify(error="Podaj nazwę"), 400
    with db() as c:
        c.execute("UPDATE przepisy SET tytul=?,kcal=?,bialko=?,wegle=?,tluszcze=?,skladniki=?,przygotowanie=? WHERE id=?",
                  (j["tytul"].strip(), int(j["kcal"]), float(j["b"]), float(j["w"]), float(j["t"]), "\n".join(j["skladniki"]),
                   "\n".join(f"{n}. {x}" for n, x in enumerate(j["kroki"], 1)), i))
    return jsonify(ok=True)

def shop_add(c, items):
    have = {r[0] for r in c.execute("SELECT tekst FROM zakupy WHERE kupione=0").fetchall()}; n = 0
    for t in items:
        t = t.strip()
        if t and t not in have: c.execute("INSERT INTO zakupy(tekst) VALUES(?)", (t,)); have.add(t); n += 1
    return n

@app.get("/api/shop")
def shop_list():
    with db() as c: return jsonify([dict(id=r["id"], tekst=r["tekst"], kupione=r["kupione"]) for r in c.execute("SELECT * FROM zakupy ORDER BY kupione, id").fetchall()])

@app.post("/api/shop")
def shop_post():
    with db() as c: n = shop_add(c, request.json["items"])
    return jsonify(added=n)

@app.post("/api/shop/day/<d>")
def shop_day(d):
    with db() as c:
        meals(c, d); items = []
        for m in c.execute("SELECT p.skladniki s, m.porcje f FROM dzien_plan m JOIN przepisy p ON p.id=m.przepis_id WHERE m.data=? AND m.stan!='pominiety'", (d,)).fetchall():
            items += ing_lines(m["s"], m["f"])
        n = shop_add(c, items)
    return jsonify(added=n)

@app.patch("/api/shop/<int:i>")
def shop_patch(i):
    with db() as c: c.execute("UPDATE zakupy SET kupione=? WHERE id=?", (int(request.json["kupione"]), i))
    return jsonify(ok=True)

@app.delete("/api/shop/<int:i>")
def shop_del(i):
    with db() as c: c.execute("DELETE FROM zakupy WHERE id=?", (i,))
    return jsonify(ok=True)

@app.delete("/api/shop")
def shop_clear():
    with db() as c: c.execute("DELETE FROM zakupy WHERE kupione=1" if request.args.get("tylko_kupione") else "DELETE FROM zakupy")
    return jsonify(ok=True)

@app.get("/img/<int:i>")
def img(i):
    with db() as c: r = c.execute("SELECT image_url FROM przepisy WHERE id=?", (i,)).fetchone()
    u = r["image_url"] if r else ""
    m = re.match(r"data:image/(\w+);base64,(.*)", u or "", re.S)
    if not m: return Response(status=404)
    return Response(base64.b64decode(m.group(2)), mimetype=f"image/{m.group(1)}", headers={"Cache-Control": "max-age=86400"})

@app.post("/api/import-pdf")
def import_pdf():
    if _p.fitz is None: 
        return jsonify(error="Zainstaluj PyMuPDF: pip install pymupdf"), 500
    
    pdf_file = request.files.get("pdf") or request.files.get("file")
    if not pdf_file and request.files:
        pdf_file = list(request.files.values())[0]

    if not pdf_file or pdf_file.filename == "":
        return jsonify(error="Nie przesłano pliku PDF"), 400

    rs = extract_meals_and_images_from_pdf(pdf_file)
    add = upd = 0
    with db() as c:
        ex = {norm(r["tytul"]): dict(id=r["id"], sloty=r["sloty"]) for r in c.execute("SELECT id, tytul, sloty FROM przepisy").fetchall()}
        for r in rs:
            img = pack_img(r["image_url"]) if r["image_url"] else None
            nrs = slots_of(r["tytul"], r["posilek_nr"]); r["tytul"] = clean_title(r["tytul"])[0]
            k = norm(r["tytul"])
            if k in ex:
                sl = add_slot(ex[k]["sloty"], nrs); ex[k]["sloty"] = sl
                c.execute("UPDATE przepisy SET sloty=? WHERE id=?", (sl, ex[k]["id"]))
                if img: c.execute("UPDATE przepisy SET image_url=?, img_ok=1 WHERE id=?", (img, ex[k]["id"]))
                upd += 1
            else:
                cur = c.execute("""INSERT INTO przepisy(tytul,skladniki,przygotowanie,kcal,bialko,wegle,tluszcze,image_url,zrodlo,sloty,img_ok)
                    VALUES(?,?,?,?,?,?,?,?,'pdf',?,?)""", (r["tytul"], r["skladniki"], r["przygotowanie"], r["kcal"], r["bialko"], r["wegle"], r["tluszcze"],
                    img or r["image_url"], add_slot("", nrs), 1 if img else 0))
                ex[k] = dict(id=cur.lastrowid, sloty=add_slot("", nrs)); add += 1
            gc.collect()
    return jsonify(added=add, updated=upd)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)