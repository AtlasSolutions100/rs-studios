"""Delete this workspace (roadmap M7.5; wsdelete.py does the work).

    GET  /app/workspace/delete           what deleting removes and keeps, the 7-day grace, the plan check
    POST /app/workspace/delete           the owner schedules it (types the workspace name to confirm)
    POST /app/workspace/delete/cancel    the owner cancels it during the grace

Only the owner (or an operator in their own workspace; an operator's workspace itself can't be deleted). An admin
viewing another workspace is read-only. A live plan blocks it: the page sends the owner to Billing to cancel it;
nothing here cancels a plan.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import db
import wsdelete
from core import is_owner, login_required

bp = Blueprint("wsdelete", __name__)


def _owner():
    if g.get("viewing"):
        abort(403, "Viewing as admin is read-only.")
    if not is_owner():
        abort(403, "Only the workspace owner can delete the workspace.")


@bp.route("/app/workspace/delete")
@login_required
def page():
    with db.connect() as conn:
        st = wsdelete.status(conn, g.ws["id"])
    return render_template("workspace_delete.html", st=st, owner=is_owner() and not g.get("viewing"))


@bp.route("/app/workspace/delete", methods=["POST"])
@login_required
def schedule():
    _owner()
    typed = (request.form.get("confirm") or "").strip()
    if typed.casefold() != (g.ws["name"] or "").strip().casefold() or not typed:
        flash("Type the workspace name exactly as shown to confirm.", "bad")
        return redirect(url_for("wsdelete.page"))
    with db.connect() as conn:
        st = wsdelete.request(conn, g.ws["id"], g.user["id"])
    if st["blocked"] == "billing":
        flash("Cancel your plan on the Billing page first. Then you can delete the workspace.", "bad")
    elif st["blocked"]:
        flash("This workspace can't be deleted here.", "bad")
    else:
        flash("The workspace will be deleted in 7 days. You can cancel until then.")
    return redirect(url_for("wsdelete.page"))


@bp.route("/app/workspace/delete/cancel", methods=["POST"])
@login_required
def cancel():
    _owner()
    with db.connect() as conn:
        wsdelete.cancel(conn, g.ws["id"], g.user["id"])
    flash("Deletion canceled. Nothing was removed.")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/app") else
                    url_for("wsdelete.page"))
