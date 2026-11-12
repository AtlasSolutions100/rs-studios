"""Inspiration by category (Koby, Nov 11 2026): our own example ads, grouped, each one a way to start.

    groups(product)    [{"key", "name", "items": [{"title", "line", "image", "video", "alt", "ask", "href", "where"}]}]

Only our own generated work is shown: the campaign examples the desk already had (desk.EXAMPLES) and the home
page's gallery (static/showcase/manifest.json through public_site.showcase, whose rule is "no stock, no other
companies' ads, no real people"). Nothing here is fetched, generated or charged.

What picking one does, and each card says which:
  * a campaign example, or a clip that came from no preset: `ask`, a sentence written for this product, which the
    desk puts in its box (the member then reads the priced plan before anything is made);
  * an example made with a preset: `href`, Studio with that preset already picked (its price is shown there).
An example whose preset is gone still shows, as a sentence. A category with nothing in it is left out.
"""
from flask import url_for

CATS = [("campaigns", "Campaigns"), ("presenter", "Presenter videos"), ("product", "Product videos"),
        ("local", "Local businesses"), ("image", "Image ads")]
PER_CAT = 12
ASKS = {
    "presenter": "One presenter video for {name}, selfie style, talking to camera",
    "product": "A product video for {name}: the product on screen, no person",
    "local": "A short video for {name}: the work being done, no one talking",
    "image": "An image ad for {name}",
}


def _cat(item) -> str:
    """Local businesses and services have a row of their own (the manifest's group: "local"), people talking or not."""
    if item["kind"] == "image":
        return "image"
    if item.get("group") == "local":
        return "local"
    return "presenter" if item.get("presenter") else "product"


def _ask(item, cat: str) -> str:
    """The sentence a card puts in the desk's box: a presenter example asks for a presenter, wherever it is listed."""
    return ASKS["presenter"] if item.get("presenter") else ASKS[cat]


def groups(product) -> list[dict]:
    import desk
    import presets
    import public_site
    name = product["name"] if product is not None else "your product"
    by = {key: [] for key, _n in CATS}
    for e in desk.examples(product):
        by["campaigns"].append({"title": e["title"], "line": e["line"], "image": e["image"], "video": e["video"],
                                "alt": "", "ask": e["ask"], "href": "", "where": "desk"})
    for it in public_site.showcase(64):
        cat = _cat(it)
        still = it["poster"] if it["kind"] == "video" else it["src"]
        card = {"title": it["preset_name"] or it["caption"], "alt": it["alt"],
                "line": it["caption"] if it["caption"] and it["caption"] != it["preset_name"] else "",
                "image": url_for("static", filename=still) if still else "",
                "video": url_for("static", filename=it["src"]) if it["kind"] == "video" else "",
                "ask": "", "href": "", "where": ""}
        if it["preset"] and presets.get(it["preset"]) is not None:
            card.update(href=url_for("studio_v2.page", preset=it["preset"]), where="studio")
        else:
            card.update(ask=_ask(it, cat).format(name=name), where="desk")
        if len(by[cat]) < PER_CAT:
            by[cat].append(card)
    return [{"key": key, "name": label, "items": by[key]} for key, label in CATS if by[key]]
