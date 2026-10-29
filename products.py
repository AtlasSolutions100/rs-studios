"""Brands and products: what every creative flow starts from.

A workspace has brands (how many depends on the plan) and each brand has any
number of products. One brand and one product are *active* at a time
(workspaces.brand_id / product_id); every flow defaults to them and can pick
another product instead.

The older "brand kit" columns are kept as a mirror of the active brand and
product (workspaces.brand_name/product_name/claims/audience, and each brand's
product_name/claims/audience for the product it last had active), so code that
reads them keeps working. `sync_mirror()` keeps them current.

No Flask routes here (product_routes.py has them); `g` is only read by the
helpers that say so.
"""
import json

import compliance
import db
import plans

# (field, max length) a product form may set.
FIELDS = (("name", 80), ("description", 600), ("price", 60), ("offer", 200), ("claims", 2000),
          ("audience", 300), ("url", 500), ("tags", 200))
BRAND_FIELDS = (("brand_name", 80), ("about", 600), ("website", 300), ("tone", 80), ("audience", 300))
MAX_PENDING = 20


class ProductError(ValueError):
    pass


# ---------------------------------------------------------------- reads

def brands(conn, ws_id):
    return conn.execute("SELECT * FROM brands WHERE workspace_id=? ORDER BY id", (ws_id,)).fetchall()


def products(conn, ws_id, brand_id=None):
    if brand_id:
        return conn.execute("SELECT * FROM products WHERE workspace_id=? AND brand_id=? AND archived=0 ORDER BY id",
                            (ws_id, brand_id)).fetchall()
    return conn.execute("SELECT * FROM products WHERE workspace_id=? AND archived=0 ORDER BY brand_id, id",
                        (ws_id,)).fetchall()


def get_brand(conn, ws_id, bid):
    if not bid:
        return None
    return conn.execute("SELECT * FROM brands WHERE id=? AND workspace_id=?", (bid, ws_id)).fetchone()


def get_product(conn, ws_id, pid):
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return None
    if not pid:
        return None
    return conn.execute("SELECT * FROM products WHERE id=? AND workspace_id=? AND archived=0", (pid, ws_id)).fetchone()


def unlocked_brand_ids(conn, ws) -> set:
    """Brands the plan lets this workspace use: the oldest N (all on unlimited
    plans). Brands made on a bigger plan stay, read-only, after a downgrade."""
    ids = [b["id"] for b in brands(conn, ws["id"])]
    cap = plans.limit(ws, "brands")
    return set(ids if cap is None else ids[:cap])


def tree(conn, ws):
    """[{brand, products, locked}] for the switcher and the product pickers."""
    ok = unlocked_brand_ids(conn, ws)
    by_brand = {}
    for p in products(conn, ws["id"]):
        by_brand.setdefault(p["brand_id"], []).append(p)
    return [{"brand": b, "products": by_brand.get(b["id"], []), "locked": b["id"] not in ok}
            for b in brands(conn, ws["id"])]


def photos_for(conn, ws_id, product_id=None, everything=False):
    """A product's photos (primary first, then newest), optionally followed by
    the workspace's other photos."""
    rows = []
    if product_id:
        p = get_product(conn, ws_id, product_id)
        prim = p["primary_photo_id"] if p else None
        rows = conn.execute("SELECT * FROM photos WHERE workspace_id=? AND product_id=? ORDER BY (id=?) DESC, id DESC",
                            (ws_id, product_id, prim or 0)).fetchall()
    if everything or not product_id:
        seen = {r["id"] for r in rows}
        rows += [r for r in conn.execute("SELECT * FROM photos WHERE workspace_id=? ORDER BY id DESC", (ws_id,))
                 if r["id"] not in seen]
    return rows


def pending(product) -> list:
    try:
        return [c for c in json.loads(product["claims_pending"] or "[]") if isinstance(c, str)]
    except (ValueError, TypeError):
        return []


def import_info(product) -> dict:
    try:
        return json.loads(product["import_json"] or "{}") or {}
    except (ValueError, TypeError):
        return {}


def claims_list(text) -> list:
    return [c.strip(" -•\t") for c in (text or "").splitlines() if c.strip(" -•\t")]


def as_brief(brand_row, product_row, ws) -> dict:
    """The dict every flow reads (core.brand()): brand name, product name,
    approved claims, audience, plus the product's details."""
    b, p = brand_row, product_row
    get = (lambda r, k: (r[k] if r is not None and k in r.keys() else None) or "")
    if b is None and p is None:   # legacy: the workspace's own columns
        return {"name": ws["brand_name"] or ws["name"], "product": ws["product_name"] or "", "claims": ws["claims"] or "",
                "audience": ws["audience"] or "", "description": "", "price": "", "offer": "", "url": "", "tags": "",
                "about": "", "tone": "", "website": "", "brand_id": None, "product_id": None}
    return {"name": get(b, "brand_name") or ws["name"], "product": get(p, "name"), "claims": get(p, "claims"),
            "audience": get(p, "audience") or get(b, "audience"), "description": get(p, "description"),
            "price": get(p, "price"), "offer": get(p, "offer"), "url": get(p, "url") or get(b, "website"),
            "tags": get(p, "tags"), "about": get(b, "about"), "tone": get(b, "tone"), "website": get(b, "website"),
            "brand_id": b["id"] if b is not None else None, "product_id": p["id"] if p is not None else None}


def brief_for(conn, ws, product_id=None):
    """as_brief() for a given product of this workspace (or the active one)."""
    p = get_product(conn, ws["id"], product_id) if product_id else None
    if p is None:
        p = get_product(conn, ws["id"], ws["product_id"])
    b = get_brand(conn, ws["id"], p["brand_id"] if p is not None else ws["brand_id"])
    return as_brief(b, p, ws)


# ---------------------------------------------------------------- active brand/product

def ensure_brand(conn, ws_id) -> int:
    """Every workspace has at least one brand row (made from its own columns)."""
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if ws["brand_id"] and get_brand(conn, ws_id, ws["brand_id"]):
        return ws["brand_id"]
    first = conn.execute("SELECT id FROM brands WHERE workspace_id=? ORDER BY id LIMIT 1", (ws_id,)).fetchone()
    bid = first["id"] if first else conn.execute(
        "INSERT INTO brands (workspace_id, brand_name, product_name, claims, audience, created_at) VALUES (?,?,?,?,?,?)",
        (ws_id, ws["brand_name"], ws["product_name"], ws["claims"], ws["audience"], db.now())).lastrowid
    conn.execute("UPDATE workspaces SET brand_id=? WHERE id=?", (bid, ws_id))
    conn.commit()
    return bid


def load_active(conn, ws):
    """(brand row, product row) for the workspace, repairing stale pointers."""
    bid = ensure_brand(conn, ws["id"])
    b = get_brand(conn, ws["id"], bid)
    p = get_product(conn, ws["id"], ws["product_id"])
    if p is not None and p["brand_id"] != b["id"]:
        p = None
    if p is None:
        p = get_product(conn, ws["id"], b["product_id"])
        if p is None or p["brand_id"] != b["id"]:
            rows = products(conn, ws["id"], b["id"])
            p = rows[0] if rows else None
        if (p["id"] if p else None) != ws["product_id"]:
            conn.execute("UPDATE workspaces SET product_id=? WHERE id=?", (p["id"] if p else None, ws["id"]))
            conn.commit()
    return b, p


def activate(conn, ws, brand_id=None, product_id=None):
    """Make a product (or a brand, with its last-used product) the active one.
    Raises ProductError when it isn't this workspace's or the plan locks it."""
    p = None
    if product_id:
        p = get_product(conn, ws["id"], product_id)
        if p is None:
            raise ProductError("not found")
        brand_id = p["brand_id"]
    b = get_brand(conn, ws["id"], brand_id)
    if b is None:
        raise ProductError("not found")
    if b["id"] not in unlocked_brand_ids(conn, ws):
        cap = plans.limit(ws, "brands")
        raise ProductError(f"Your plan includes {cap} brand{'s' if cap != 1 else ''}. Upgrade to work on "
                           f"{b['brand_name'] or 'this brand'} again.")
    if p is None:
        p = get_product(conn, ws["id"], b["product_id"])
        if p is None:
            rows = products(conn, ws["id"], b["id"])
            p = rows[0] if rows else None
    conn.execute("UPDATE workspaces SET brand_id=?, product_id=? WHERE id=?", (b["id"], p["id"] if p else None, ws["id"]))
    if p is not None:
        conn.execute("UPDATE brands SET product_id=? WHERE id=?", (p["id"], b["id"]))
    conn.commit()
    sync_mirror(conn, ws["id"])
    return b, p


def sync_mirror(conn, ws_id):
    """Copy the active brand + product into the legacy brand-kit columns."""
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    b = get_brand(conn, ws_id, ws["brand_id"])
    if b is None:
        return
    p = get_product(conn, ws_id, b["product_id"]) or get_product(conn, ws_id, ws["product_id"])
    if p is not None and p["brand_id"] != b["id"]:
        p = None
    aud = ((p["audience"] if p is not None else "") or b["audience"] or "")
    if p is not None:
        conn.execute("UPDATE brands SET product_name=?, claims=? WHERE id=?", (p["name"], p["claims"], b["id"]))
    conn.execute("UPDATE workspaces SET brand_name=?, product_name=?, claims=?, audience=? WHERE id=?",
                 (b["brand_name"], p["name"] if p is not None else b["product_name"],
                  p["claims"] if p is not None else b["claims"], aud, ws_id))
    conn.commit()


# ---------------------------------------------------------------- writes

def clean(form, fields=FIELDS) -> dict:
    out = {}
    for k, lim in fields:
        if k in form:
            v = form.get(k)
            out[k] = (v if isinstance(v, str) else "" if v is None else str(v)).strip()[:lim]
    if "tags" in out:
        out["tags"] = ", ".join(dict.fromkeys(t.strip().lower() for t in out["tags"].split(",") if t.strip()))[:200]
    return out


def check_fields(vals: dict):
    """Compliance on everything a product or brand may put in an ad."""
    chk = compliance.check(*[vals.get(k) or "" for k in ("brand_name", "name", "description", "offer", "claims",
                                                          "audience", "about")])
    if not chk["ok"]:
        raise ProductError("Not saved: " + chk["blocks"][0])
    return chk


def create_brand(conn, ws, name, **fields) -> int:
    """A new brand (plan limit enforced here, server-side)."""
    vals = clean(dict(fields, brand_name=name), BRAND_FIELDS)
    if not vals.get("brand_name"):
        raise ProductError("Give the new brand a name.")
    check_fields(vals)
    n = len(brands(conn, ws["id"]))
    if not plans.within(ws, "brands", n):
        cap = plans.limit(ws, "brands")
        nxt = plans.next_plan_for("brands", n)
        raise ProductError(f"Your plan includes {cap} brand{'s' if cap != 1 else ''}."
                           + (f" {nxt['name']} includes more." if nxt else ""))
    cols = ["workspace_id", "created_at"] + list(vals)
    bid = conn.execute(f"INSERT INTO brands ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       (ws["id"], db.now(), *vals.values())).lastrowid
    conn.commit()
    return bid


def update_brand(conn, ws_id, bid, form):
    vals = clean(form, BRAND_FIELDS)
    if "brand_name" in vals and not vals["brand_name"]:
        raise ProductError("A brand needs a name.")
    check_fields(vals)
    if vals:
        conn.execute(f"UPDATE brands SET {', '.join(k + '=?' for k in vals)} WHERE id=? AND workspace_id=?",
                     (*vals.values(), bid, ws_id))
        conn.commit()
    sync_mirror(conn, ws_id)


def create_product(conn, ws, brand_id, form, pending_claims=None, import_json=None) -> int:
    b = get_brand(conn, ws["id"], brand_id)
    if b is None:
        raise ProductError("not found")
    if b["id"] not in unlocked_brand_ids(conn, ws):
        raise ProductError("That brand is locked on your plan. Upgrade to add products to it.")
    vals = clean(form)
    if not vals.get("name"):
        raise ProductError("Give the product a name.")
    check_fields(vals)
    t = db.now()
    pend = [c for c in (pending_claims or []) if compliance.check(c)["ok"]][:MAX_PENDING]
    first_in_ws = not conn.execute("SELECT 1 FROM products WHERE workspace_id=?", (ws["id"],)).fetchone()
    cols = ["workspace_id", "brand_id", "created_at", "updated_at", "claims_pending", "import_json"] + list(vals)
    pid = conn.execute(f"INSERT INTO products ({', '.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       (ws["id"], b["id"], t, t, json.dumps(pend), json.dumps(import_json) if import_json else None,
                        *vals.values())).lastrowid
    if first_in_ws and len(brands(conn, ws["id"])) == 1:
        # Photos uploaded before there was any product belong to this one.
        conn.execute("UPDATE photos SET product_id=? WHERE workspace_id=? AND product_id IS NULL", (pid, ws["id"]))
        newest = conn.execute("SELECT MAX(id) FROM photos WHERE product_id=?", (pid,)).fetchone()[0]
        if newest:
            conn.execute("UPDATE products SET primary_photo_id=? WHERE id=?", (newest, pid))
    conn.commit()
    return pid


def update_product(conn, ws_id, pid, form):
    vals = clean(form)
    if "name" in vals and not vals["name"]:
        raise ProductError("A product needs a name.")
    check_fields(vals)
    if vals:
        conn.execute(f"UPDATE products SET {', '.join(k + '=?' for k in vals)}, updated_at=? WHERE id=? AND workspace_id=?",
                     (*vals.values(), db.now(), pid, ws_id))
        conn.commit()
    sync_mirror(conn, ws_id)


def set_pending(conn, pid, items):
    conn.execute("UPDATE products SET claims_pending=?, updated_at=? WHERE id=?",
                 (json.dumps(items[:MAX_PENDING]), db.now(), pid))
    conn.commit()


def approve_claims(conn, ws_id, pid, picks: list):
    """Move suggested claims (by index) into the approved list. Only the
    member approves claims; nothing AI-suggested is used until then."""
    p = get_product(conn, ws_id, pid)
    pend = pending(p)
    take = [pend[i] for i in sorted(set(picks)) if 0 <= i < len(pend)]
    have = claims_list(p["claims"])
    new = have + [c for c in take if c not in have]
    check_fields({"claims": "\n".join(new)})
    conn.execute("UPDATE products SET claims=?, updated_at=? WHERE id=?", ("\n".join(new)[:2000], db.now(), pid))
    conn.commit()
    set_pending(conn, pid, [c for i, c in enumerate(pend) if c not in take])
    sync_mirror(conn, ws_id)
    return take


def dismiss_claims(conn, ws_id, pid, picks: list):
    p = get_product(conn, ws_id, pid)
    pend = pending(p)
    set_pending(conn, pid, [c for i, c in enumerate(pend) if i not in set(picks)])


def archive_product(conn, ws, pid):
    p = get_product(conn, ws["id"], pid)
    if p is None:
        raise ProductError("not found")
    conn.execute("UPDATE products SET archived=1, updated_at=? WHERE id=?", (db.now(), pid))
    conn.execute("UPDATE brands SET product_id=NULL WHERE product_id=?", (pid,))
    if ws["product_id"] == pid:
        conn.execute("UPDATE workspaces SET product_id=NULL WHERE id=?", (ws["id"],))
    conn.commit()
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
    load_active(conn, ws)
    sync_mirror(conn, ws["id"])


def save_kit(conn, ws, brand_name=None, product_name=None, claims=None, audience=None, first_ad=False):
    """The one-form brand kit (POST /app/brand) and the first-ad flow: writes
    the active brand and its product, creating the product when there isn't
    one. `first_ad` only fills gaps: it never renames the brand to blank,
    keeps an audience already set, and adds a product with a new name instead
    of renaming the active one. Returns the product id (or None)."""
    check_fields({"brand_name": brand_name or "", "name": product_name or "", "claims": claims or "",
                  "audience": audience or ""})
    ensure_brand(conn, ws["id"])
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
    b, p = load_active(conn, ws)
    if first_ad:
        if brand_name:
            conn.execute("UPDATE brands SET brand_name=? WHERE id=?", (brand_name[:80], b["id"]))
        if audience:
            conn.execute("UPDATE brands SET audience=COALESCE(NULLIF(audience,''), ?) WHERE id=?", (audience[:300], b["id"]))
    else:
        if brand_name is not None:
            conn.execute("UPDATE brands SET brand_name=? WHERE id=?", (brand_name[:80], b["id"]))
        if audience is not None:
            conn.execute("UPDATE brands SET audience=? WHERE id=?", (audience[:300], b["id"]))
    conn.commit()
    pid = p["id"] if p is not None else None
    if first_ad:
        same = p is not None and p["name"] == product_name
        row = p if same else conn.execute("SELECT * FROM products WHERE workspace_id=? AND brand_id=? AND name=? AND archived=0",
                                          (ws["id"], b["id"], product_name)).fetchone()
        if row is not None:
            pid = row["id"]
            if audience and not row["audience"]:
                update_product(conn, ws["id"], pid, {"audience": audience})
        else:
            pid = create_product(conn, ws, b["id"], {"name": product_name, "audience": audience or ""})
    elif pid:
        form = {k: v for k, v in (("name", product_name), ("claims", claims), ("audience", audience)) if v is not None}
        if not form.get("name", "x"):
            form.pop("name")
        update_product(conn, ws["id"], pid, form)
    elif product_name:
        pid = create_product(conn, ws, b["id"], {"name": product_name, "claims": claims or "", "audience": audience or ""})
    if pid:
        conn.execute("UPDATE workspaces SET product_id=? WHERE id=?", (pid, ws["id"]))
        conn.execute("UPDATE brands SET product_id=? WHERE id=?", (pid, b["id"]))
        conn.commit()
    sync_mirror(conn, ws["id"])
    return pid


def add_photo(conn, ws_id, product_id, path, filename, ctype, remote_url=None) -> int:
    phid = conn.execute("INSERT INTO photos (workspace_id, product_id, path, filename, content_type, remote_url, created_at) "
                        "VALUES (?,?,?,?,?,?,?)", (ws_id, product_id, path, (filename or "")[:120], ctype, remote_url,
                                                   db.now())).lastrowid
    if product_id:
        conn.execute("UPDATE products SET primary_photo_id=? WHERE id=? AND primary_photo_id IS NULL", (phid, product_id))
    conn.commit()
    return phid


def remove_photo(conn, ws_id, phid):
    p = conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (phid, ws_id)).fetchone()
    if not p:
        return None
    conn.execute("DELETE FROM photos WHERE id=?", (phid,))
    for prod in conn.execute("SELECT id FROM products WHERE primary_photo_id=?", (phid,)).fetchall():
        nxt = conn.execute("SELECT MAX(id) FROM photos WHERE product_id=?", (prod["id"],)).fetchone()[0]
        conn.execute("UPDATE products SET primary_photo_id=? WHERE id=?", (nxt, prod["id"]))
    conn.commit()
    return p


def counts(conn, ws_id) -> dict:
    """{product_id: {"photos": n, "ads": n}} for product cards."""
    out = {}
    for r in conn.execute("SELECT product_id, COUNT(*) n FROM photos WHERE workspace_id=? AND product_id IS NOT NULL "
                          "GROUP BY product_id", (ws_id,)):
        out.setdefault(r["product_id"], {"photos": 0, "ads": 0})["photos"] = r["n"]
    for r in conn.execute("SELECT product_id, COUNT(*) n FROM jobs WHERE workspace_id=? AND product_id IS NOT NULL "
                          "GROUP BY product_id", (ws_id,)):
        out.setdefault(r["product_id"], {"photos": 0, "ads": 0})["ads"] = r["n"]
    return out
