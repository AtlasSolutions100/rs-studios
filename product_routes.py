"""Products and brands pages: add/edit a product (photos, claims, details),
import one from its URL, edit a brand, and the one-click switcher."""
import json
import os
import uuid

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import db
import fetcher
import llm
import plans
import product_import
import products
from core import MAX_PHOTO_MB, PHOTO_TYPES, UPLOADS, can_afford, charge_ai, login_required, not_enough, save_upload

bp = Blueprint("products", __name__)
IMPORTS_PER_HOUR = 30   # product-page imports per workspace (each one fetches a member-supplied URL)


def _back(default):
    nxt = request.form.get("back") or request.args.get("back") or ""
    return nxt if nxt.startswith("/app") and not nxt.startswith("//") else default


def _product(pid):
    with db.connect() as conn:
        p = products.get_product(conn, g.ws["id"], pid)
    if p is None:
        abort(404)
    return p


def _locked(conn, brand_id):
    return brand_id not in products.unlocked_brand_ids(conn, g.ws)


@bp.route("/app/switch", methods=["POST"])
@login_required
def switch():
    """Make a product (or a brand) the active one, then go back where you were."""
    with db.connect() as conn:
        try:
            b, p = products.activate(conn, g.ws, brand_id=request.form.get("brand", type=int),
                                     product_id=request.form.get("product", type=int))
        except products.ProductError as e:
            if str(e) == "not found":
                abort(404)
            flash(str(e))
            return redirect(_back(url_for("brand_kit")))
    if request.headers.get("Accept", "").startswith("application/json"):
        return jsonify({"ok": True, "brand": b["id"], "product": p["id"] if p else None})
    return redirect(_back(url_for("studio")))


def _manual_ctx(b, src, why):
    """What the by-hand form shows after an import couldn't read the store:
    why, and a first guess at the brand (from the domain) and the product's
    name (from the link)."""
    if why not in ("blocked", "failed"):
        return None
    link = src if src.lower().startswith(("http://", "https://")) else ""      # never a javascript: link
    return {"why": why, "src": src, "link": link, "domain": product_import.domain(src),
            "brand": "" if (b["brand_name"] or "").strip() else product_import.brand_guess(src)}


@bp.route("/app/products/new", methods=["GET", "POST"])
@login_required
def new():
    with db.connect() as conn:
        bid = request.values.get("brand", type=int) or g.ws["brand_id"]
        b = products.get_brand(conn, g.ws["id"], bid)
        if b is None:
            abort(404)
        src = (request.values.get("src") or "").strip()[:500]
        manual = _manual_ctx(b, src, request.values.get("manual") or "")
        if request.method == "POST":
            try:
                if _locked(conn, b["id"]):
                    raise products.ProductError("That brand is locked on your plan. Upgrade to add products to it.")
                pid, charged = _create_by_hand(conn, b, request.form, manual)
                products.activate(conn, g.ws, product_id=pid)
            except products.ProductError as e:
                flash(str(e))
                return render_template("product_edit.html", p=None, b=b, form=request.form, photos=[], pend=[],
                                       imp={}, angles=[], scripts=[], manual=manual,
                                       import_quote=product_import.estimate(), ai_on=llm.anthropic_ready()), 400
            n = _save_photos(conn, pid)
            if manual:
                product_import.log_attempt(conn, g.ws["id"], manual["src"] or request.form.get("url") or "", "manual", "ok",
                                           detail=("text" if (request.form.get("page_text") or "").strip() else "form")
                                           + f", {n} photo{'s' if n != 1 else ''}")
            p = products.get_product(conn, g.ws["id"], pid)
            pend = len(products.pending(p))
            flash("Product added. It's your active product now, so every flow starts from it."
                  + (f" {pend} claim{'s' if pend != 1 else ''} from the text you pasted wait for your approval." if pend else "")
                  + (f" Reading the text cost {charged} credit{'s' if charged != 1 else ''}." if charged else ""))
            return redirect(url_for("products.edit", pid=pid))
    form = {}
    if manual:
        form = {"name": product_import.name_guess(src), "url": src}
    return render_template("product_edit.html", p=None, b=b, form=form, photos=[], pend=[], imp={}, angles=[], scripts=[],
                           import_quote=product_import.estimate(), ai_on=llm.anthropic_ready(), manual=manual)


PASTE_MAX = 20000


def _create_by_hand(conn, b, form, manual):
    """A product from the form. With pasted page text, the same AI read an
    import uses fills what the member left empty, and its claims (only ones the
    text states) wait for approval; what the member typed always wins.
    Returns (product id, credits charged)."""
    vals = {k: form.get(k) for k in form if k not in ("csrf", "brand", "page_text", "brand_name", "manual", "src")}
    pasted = (form.get("page_text") or "").strip()[:PASTE_MAX]
    pending, charged, brand = [], 0, (form.get("brand_name") or "").strip()[:80]
    if pasted:
        facts = product_import.text_facts(pasted, (vals.get("url") or "").strip(), (vals.get("name") or "").strip())
        use_ai = llm.anthropic_ready() and can_afford(product_import.estimate())
        if llm.anthropic_ready() and not use_ai:
            flash("Not enough credits for the AI read of the text, so we filled in what we could without it.")
        ai, charged = _ai_read(conn, facts, use_ai)
        built = product_import.build(facts, ai)
        typed = set(ln.strip() for ln in (vals.get("claims") or "").splitlines() if ln.strip())
        for k, v in built["form"].items():
            if k == "name" and not ai and not (vals.get("name") or "").strip():
                v = facts["name"] or ""
            if not (vals.get(k) or "").strip() and v and v != "Imported product":
                vals[k] = v
        pending = [c for c in built["pending"] if c not in typed]
        brand = brand or built["brand"]
    if not (vals.get("name") or "").strip():
        raise products.ProductError("Give the product a name.")
    if manual and brand:
        _name_brand(conn, b["id"], brand)
    pid = products.create_product(conn, g.ws, b["id"], vals, pending_claims=pending,
                                  import_json={"url": vals.get("url") or "", "images": [], "own": [], "taken": [],
                                               "by_hand": True} if manual else None)
    return pid, charged


def _save_photos(conn, pid):
    n = 0
    for fs in request.files.getlist("photos")[:20]:
        if not fs or not fs.filename:
            continue
        try:
            path, ctype = save_upload(fs, os.path.join(UPLOADS, str(g.ws["id"])), PHOTO_TYPES, MAX_PHOTO_MB)
        except ValueError as e:
            flash(f"{fs.filename}: {e}")
            continue
        products.add_photo(conn, g.ws["id"], pid, path, fs.filename, ctype)
        n += 1
    return n


@bp.route("/app/products/<int:pid>", methods=["GET", "POST"])
@login_required
def edit(pid):
    p = _product(pid)
    with db.connect() as conn:
        b = products.get_brand(conn, g.ws["id"], p["brand_id"])
        if request.method == "POST":
            try:
                if _locked(conn, b["id"]):
                    raise products.ProductError("That brand is locked on your plan.")
                products.update_product(conn, g.ws["id"], pid, request.form)
                flash("Saved.", "ok")
            except products.ProductError as e:
                flash(str(e))
            return redirect(url_for("products.edit", pid=pid))
        photos = conn.execute("SELECT * FROM photos WHERE product_id=? AND workspace_id=? ORDER BY (id=?) DESC, id DESC",
                              (pid, g.ws["id"], p["primary_photo_id"] or 0)).fetchall()
        angles = conn.execute("SELECT * FROM angles WHERE product_id=? AND workspace_id=? ORDER BY id DESC LIMIT 40",
                              (pid, g.ws["id"])).fetchall()
        scripts = conn.execute("SELECT * FROM scripts WHERE product_id=? AND workspace_id=? ORDER BY id DESC LIMIT 40",
                               (pid, g.ws["id"])).fetchall()
        ads = conn.execute("SELECT COUNT(*) FROM jobs WHERE product_id=? AND workspace_id=?", (pid, g.ws["id"])).fetchone()[0]
    imp = products.import_info(p)
    return render_template("product_edit.html", p=p, b=b, form=p, photos=photos, pend=products.pending(p), imp=imp,
                           angles=angles, scripts=scripts, ads=ads,
                           candidates=products.import_candidates(p)[0], more_candidates=products.import_candidates(p)[1])


@bp.route("/app/products/<int:pid>/photos/<int:phid>/primary", methods=["POST"])
@login_required
def primary(pid, phid):
    _product(pid)
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM photos WHERE id=? AND product_id=? AND workspace_id=?",
                        (phid, pid, g.ws["id"])).fetchone():
            conn.execute("UPDATE products SET primary_photo_id=? WHERE id=?", (phid, pid))
            conn.commit()
    return redirect(url_for("products.edit", pid=pid) + "#photos")


@bp.route("/app/products/<int:pid>/adopt", methods=["POST"])
@login_required
def adopt(pid):
    """Attach the workspace's photos that have no product to this one."""
    _product(pid)
    with db.connect() as conn:
        n = conn.execute("UPDATE photos SET product_id=? WHERE workspace_id=? AND product_id IS NULL",
                         (pid, g.ws["id"])).rowcount
        conn.execute("UPDATE products SET primary_photo_id=COALESCE(primary_photo_id, (SELECT MAX(id) FROM photos "
                     "WHERE product_id=?)) WHERE id=?", (pid, pid))
        conn.commit()
    flash(f"Moved {n} photo{'s' if n != 1 else ''}.")
    return redirect(url_for("products.edit", pid=pid) + "#photos")


@bp.route("/app/products/<int:pid>/claims", methods=["POST"])
@login_required
def claims(pid):
    """Approve or dismiss suggested claims (only the member approves claims)."""
    p = _product(pid)
    picks = [int(i) for i in request.form.getlist("i") if i.isdigit()]
    act = request.form.get("action")
    if act == "approve_all":
        act, picks = "approve", list(range(len(products.pending(p))))
    with db.connect() as conn:
        try:
            if act == "approve":
                took = products.approve_claims(conn, g.ws["id"], pid, picks)
                flash(f"Approved {len(took)} claim{'s' if len(took) != 1 else ''}. Ads and copy may use "
                      f"{'them' if len(took) != 1 else 'it'} now.")
            elif act == "dismiss":
                products.dismiss_claims(conn, g.ws["id"], pid, picks)
        except products.ProductError as e:
            flash(str(e))
    return redirect(url_for("products.edit", pid=pid) + "#claims")


@bp.route("/app/products/<int:pid>/archive", methods=["POST"])
@login_required
def archive(pid):
    _product(pid)
    with db.connect() as conn:
        products.archive_product(conn, g.ws, pid)
    flash("Product removed. Ads made for it stay in your library.")
    return redirect(url_for("brand_kit"))


@bp.route("/app/products/<int:pid>/angles/<int:aid>/delete", methods=["POST"])
@login_required
def delete_angle(pid, aid):
    with db.connect() as conn:
        conn.execute("DELETE FROM angles WHERE id=? AND product_id=? AND workspace_id=?", (aid, pid, g.ws["id"]))
        conn.commit()
    return redirect(_back(url_for("products.edit", pid=pid) + "#saved"))


@bp.route("/app/products/<int:pid>/scripts/<int:sid>/delete", methods=["POST"])
@login_required
def delete_script(pid, sid):
    with db.connect() as conn:
        conn.execute("DELETE FROM scripts WHERE id=? AND product_id=? AND workspace_id=?", (sid, pid, g.ws["id"]))
        conn.commit()
    return redirect(_back(url_for("products.edit", pid=pid) + "#saved"))


# ---------------------------------------------------------------- import from URL

def read_store(url):
    """(facts, method) for a product link, read honestly with our own
    user-agent (fetcher.UA): the store's published product data first
    (Shopify's <product-url>.js / .json), then the page itself for its JSON-LD
    Product, Open Graph tags and text. The first refusal (401/403/429 or a bot
    challenge) stops the import: product_import.Blocked, nothing retried or
    disguised. Other failures raise fetcher.FetchError."""
    fetcher.check_url(url)      # the SSRF guard runs again on every request (fetcher._get)
    shop = None
    for ep in product_import.shopify_endpoints(url):
        try:
            r = fetcher.fetch(ep, accept="application/json")
        except fetcher.FetchError:
            break               # the page request below gives the member the real reason
        if product_import.refused(r):
            raise product_import.Blocked(r["status"], "shopify-json")
        if r["status"] < 400:
            shop = product_import.shopify_facts(r["text"], url, r["content_type"])
            if shop:
                break
    page = fetcher.fetch(url)
    if product_import.refused(page):
        if shop:                # the store published this product's data; that's enough, and we stop asking
            return shop, "shopify-json"
        raise product_import.Blocked(page["status"], "page")
    if page["status"] >= 400:
        if shop:
            return shop, "shopify-json"
        raise fetcher.FetchError(f"The page answered {page['status']}.")
    if "html" not in (page["content_type"] or "").lower() and "<html" not in page["text"][:2000].lower():
        if shop:
            return shop, "shopify-json"
        raise fetcher.FetchError("That link isn't a web page.")
    facts = product_import.extract(page["text"], page["url"])
    if shop:
        return product_import.merge(facts, shop), "shopify-json"
    sj = "" if product_import.shopify_endpoints(url) else product_import.shopify_json_url(page["url"], page["text"])
    if sj:
        # A Shopify page reached from another link (a redirect): its product JSON lists exactly this product's photos.
        try:
            r = fetcher.fetch(sj, accept="application/json")
            if not product_import.refused(r) and r["status"] < 400:
                product_import.prefer_images(facts, product_import.shopify_images(r["text"]))
        except fetcher.FetchError:
            pass
    return facts, product_import.method_of(facts)


def _ai_read(conn, facts, use_ai):
    ai, charged = None, 0
    if use_ai and llm.anthropic_ready():
        usage = llm.Usage()
        try:
            ai = product_import.suggest(facts, usage)
        except llm.LLMError:
            ai = None
        if usage.calls:
            charged = charge_ai(conn, usage.credits, "AI product import")
            conn.commit()
    return ai, charged


def _name_brand(conn, brand_id, name):
    b = products.get_brand(conn, g.ws["id"], brand_id)
    if b is not None and not (b["brand_name"] or "").strip() and (name or "").strip():
        try:
            products.update_brand(conn, g.ws["id"], b["id"], {"brand_name": name.strip()})
        except products.ProductError:
            pass


def import_url(conn, url, brand_id, use_ai=True):
    """Fetch a product page and make a product from it (shared with the setup
    assistant). Returns (product id, facts, credits charged). Raises
    product_import.Blocked (the store refuses apps), fetcher.FetchError or
    products.ProductError. Every try is logged for the admin card."""
    if _locked(conn, brand_id):
        raise products.ProductError("That brand is locked on your plan.")
    try:
        facts, method = read_store(url)
    except product_import.Blocked as e:
        product_import.log_attempt(conn, g.ws["id"], url, e.method, "blocked", e.status)
        raise
    except fetcher.FetchError as e:
        product_import.log_attempt(conn, g.ws["id"], url, "page", "failed", detail=str(e))
        raise
    ai, charged = _ai_read(conn, facts, use_ai)
    built = product_import.build(facts, ai)
    _name_brand(conn, brand_id, built["brand"])
    try:
        pid = products.create_product(conn, g.ws, brand_id, built["form"], pending_claims=built["pending"],
                                      import_json=built["import"])
    except products.ProductError as e:
        product_import.log_attempt(conn, g.ws["id"], url, method, "failed", detail=str(e))
        raise
    product_import.log_attempt(conn, g.ws["id"], url, method, "ok")
    return pid, facts, charged


def manual_url(bid, url, why="blocked"):
    """The add-product page with the by-hand form open and prefilled from the link."""
    return url_for("products.new", brand=bid, manual=why, src=(url or "")[:500]) + "#details"


@bp.route("/app/products/import", methods=["POST"])
@login_required
def import_page():
    url = (request.form.get("url") or "").strip()[:500]
    bid = request.form.get("brand", type=int) or g.ws["brand_id"]
    use_ai = request.form.get("ai", "1") == "1"
    key = f"import:{g.ws['id']}"
    if db.throttled(key, IMPORTS_PER_HOUR, 3600):
        flash("That's a lot of imports this hour. Try again in a little while.")
        return redirect(url_for("products.new", brand=bid))
    db.hit(key)
    if use_ai and llm.anthropic_ready() and not can_afford(product_import.estimate()):
        not_enough(product_import.estimate())
        return redirect(url_for("products.new", brand=bid))
    with db.connect() as conn:
        if products.get_brand(conn, g.ws["id"], bid) is None:
            abort(404)
        try:
            pid, facts, charged = import_url(conn, url, bid, use_ai=use_ai)
            products.activate(conn, g.ws, product_id=pid)
        except product_import.Blocked:
            return redirect(manual_url(bid, url, "blocked"))
        except fetcher.FetchError as e:
            if "private or internal" in str(e) or "Only " in str(e) or "username or password" in str(e) \
                    or "Add a URL" in str(e):
                flash(f"Couldn't import that page: {e}")      # not a store link we may read: no form prefilled from it
                return redirect(url_for("products.new", brand=bid))
            flash(f"Couldn't read that page: {e}")
            return redirect(manual_url(bid, url, "failed"))
        except products.ProductError as e:
            flash(f"Couldn't import that page: {e}")
            return redirect(url_for("products.new", brand=bid))
    flash("Imported. Check the details, approve the claims you're allowed to make, and pick the photos to keep."
          + (f" The AI read cost {charged} credit{'s' if charged != 1 else ''}." if charged else ""))
    return redirect(url_for("products.edit", pid=pid))


def add_candidate_photos(conn, ws_id, p, urls) -> tuple[int, int]:
    """Download photos the page import found (only those: never an arbitrary
    URL) onto the product. Used by the product page and the setup guide.
    Returns (added, failed)."""
    imp = products.import_info(p)
    picks = [u for u in dict.fromkeys(urls or []) if u in imp.get("images", []) and u not in imp.get("taken", [])][:10]
    folder = os.path.join(UPLOADS, str(ws_id))
    os.makedirs(folder, exist_ok=True)
    added, failed = 0, 0
    for u in picks:
        try:
            data, ctype = product_import.download_image(u)
        except fetcher.FetchError:
            failed += 1
            continue
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[ctype]
        path = os.path.join(folder, f"{uuid.uuid4().hex}{ext}")
        with open(path, "wb") as f:
            f.write(data)
        products.add_photo(conn, ws_id, p["id"], path, u.rsplit("/", 1)[-1].split("?")[0][:120], ctype)
        imp.setdefault("taken", []).append(u)
        added += 1
    conn.execute("UPDATE products SET import_json=? WHERE id=?", (json.dumps(imp), p["id"]))
    conn.commit()
    return added, failed


@bp.route("/app/products/<int:pid>/import-photos", methods=["POST"])
@login_required
def import_photos(pid):
    """Download the candidate photos the member picked (only ones the import found)."""
    p = _product(pid)
    picks = request.form.getlist("img")
    with db.connect() as conn:
        added, failed = add_candidate_photos(conn, g.ws["id"], p, picks)
    if added:
        flash(f"Added {added} photo{'s' if added != 1 else ''}." + (f" {failed} couldn't be used." if failed else ""))
    elif picks:
        flash("Those images couldn't be downloaded or are too small. Upload photos instead.")
    return redirect(url_for("products.edit", pid=pid) + "#photos")


# ---------------------------------------------------------------- brands

@bp.route("/app/brands/<int:bid>/edit", methods=["GET", "POST"])
@login_required
def brand_edit(bid):
    with db.connect() as conn:
        b = products.get_brand(conn, g.ws["id"], bid)
        if b is None:
            abort(404)
        if request.method == "POST":
            try:
                if _locked(conn, bid):
                    raise products.ProductError("That brand is locked on your plan.")
                products.update_brand(conn, g.ws["id"], bid, request.form)
                flash("Brand saved.")
                return redirect(_back(url_for("brand_kit")))
            except products.ProductError as e:
                flash(str(e))
        rows = products.products(conn, g.ws["id"], bid)
        locked = _locked(conn, bid)
    return render_template("brand_edit.html", b=b, rows=rows, locked=locked, cap=plans.limit(g.ws, "brands"))
