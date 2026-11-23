"""Manage Content Rewards / retainer / affiliate campaigns in pipeline.db.

  python -m monetization.campaign_cli add --name "GTA VI Clips" --model cpm --cpm 1.0 --cap 200 --streamer shroud
  python -m monetization.campaign_cli list|show ID|edit ID --cpm 1.5|pause ID|resume ID|link ID STREAMER
"""
import argparse
import json
import os
import sys
import time

from db.state import State

MODELS = ("cpm", "per_post", "retainer", "pool")
SOURCES = ("content_rewards", "retainer", "affiliate")
PLATFORMS = ("tiktok", "instagram", "youtube")
STATUSES = ("active", "paused", "depleted")
FREE_FEE_BUDGET = 5000.0


def fee_rate_for(model: str, total_budget_usd: float) -> float:
    """Content Rewards creator fee: 0% only for per_post/retainer with budget >= $5,000, else 10%."""
    return 0.0 if model in ("per_post", "retainer") and total_budget_usd >= FREE_FEE_BUDGET else 0.10


def resolve_streamer(db, ref) -> int:
    """Streamer by numeric id or twitch_channel (case-insensitive, leading # / @ ignored)."""
    if ref is None:
        return None
    s = str(ref).lstrip("#@")
    rows = db.query("SELECT id FROM streamers WHERE id=? OR lower(twitch_channel)=lower(?)",
                    (int(s) if s.isdigit() else -1, s))
    if not rows:
        raise SystemExit(f"error: unknown streamer {ref!r} (add it first)")
    return rows[0]["id"]


def _platforms(csv: str) -> list[str]:
    ps = [p.strip().lower() for p in csv.split(",") if p.strip()]
    bad = [p for p in ps if p not in PLATFORMS]
    if bad or not ps:
        raise SystemExit(f"error: platforms must be from {PLATFORMS}, got {csv!r}")
    return ps


def add_campaign(db, *, name, model, cpm=0.0, flat_fee=0.0, min_payout=0.0, cap=200.0, budget=1000.0,
                 fee_rate=None, window_min=30, earning_days=30, platforms="tiktok,instagram,youtube",
                 tags=None, disclosure=None, source="content_rewards", streamer=None) -> int:
    if model not in MODELS:
        raise SystemExit(f"error: model must be one of {MODELS}")
    if model in ("cpm", "pool") and cpm <= 0:
        raise SystemExit("error: --cpm must be > 0 for cpm/pool campaigns")
    if model in ("per_post", "retainer") and flat_fee <= 0:
        raise SystemExit("error: --flat-fee must be > 0 for per_post/retainer campaigns")
    if fee_rate is None:
        fee_rate = fee_rate_for(model, budget)
    cur = db.execute(
        "INSERT INTO campaigns (platform_source,name,streamer_id,payment_model,cpm_rate_usd,flat_fee_usd,"
        "min_payout_usd,max_payout_cap_usd,total_budget_usd,creator_fee_rate,submission_window_min,"
        "earning_window_days,allowed_platforms,required_tags,ad_disclosure,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, name, resolve_streamer(db, streamer), model, cpm, flat_fee, min_payout, cap, budget, fee_rate,
         window_min, earning_days, json.dumps(_platforms(platforms)), tags, disclosure, int(time.time())))
    return cur.lastrowid


# CLI flag -> column; applied by `edit`
_EDITABLE = {"name": "name", "cpm": "cpm_rate_usd", "flat_fee": "flat_fee_usd", "min_payout": "min_payout_usd",
             "cap": "max_payout_cap_usd", "budget": "total_budget_usd", "fee_rate": "creator_fee_rate",
             "window_min": "submission_window_min", "earning_days": "earning_window_days",
             "tags": "required_tags", "disclosure": "ad_disclosure", "source": "platform_source",
             "model": "payment_model"}


def edit_campaign(db, cid, **changes):
    row = get_campaign(db, cid)
    sets, vals = {}, []
    for k, col in _EDITABLE.items():
        if changes.get(k) is not None:
            sets[col] = changes[k]
    if changes.get("platforms") is not None:
        sets["allowed_platforms"] = json.dumps(_platforms(changes["platforms"]))
    if changes.get("streamer") is not None:
        sets["streamer_id"] = resolve_streamer(db, changes["streamer"])
    if "payment_model" in sets and sets["payment_model"] not in MODELS:
        raise SystemExit(f"error: model must be one of {MODELS}")
    # budget/model changes re-derive the fee unless the caller pinned it explicitly
    if "creator_fee_rate" not in sets and ("total_budget_usd" in sets or "payment_model" in sets):
        sets["creator_fee_rate"] = fee_rate_for(sets.get("payment_model", row["payment_model"]),
                                                sets.get("total_budget_usd", row["total_budget_usd"]))
    if not sets:
        raise SystemExit("error: nothing to change")
    db.execute(f"UPDATE campaigns SET {','.join(c + '=?' for c in sets)} WHERE id=?", [*sets.values(), cid])


def get_campaign(db, cid) -> dict:
    rows = db.query("SELECT c.*, s.twitch_channel FROM campaigns c LEFT JOIN streamers s ON s.id=c.streamer_id"
                    " WHERE c.id=?", (cid,))
    if not rows:
        raise SystemExit(f"error: no campaign {cid}")
    return rows[0]


def set_status(db, cid, status):
    get_campaign(db, cid)
    db.execute("UPDATE campaigns SET status=? WHERE id=?", (status, cid))


def _fmt(c: dict) -> str:
    money = (f"${c['cpm_rate_usd']:.2f} CPM, cap ${c['max_payout_cap_usd']:.0f}" if c["payment_model"] in ("cpm", "pool")
             else f"${c['flat_fee_usd']:.2f} flat")
    return (f"#{c['id']:<3} [{c['status']}] {c['name']} — {c['payment_model']} {money}, fee "
            f"{c['creator_fee_rate']:.0%}, platforms {','.join(json.loads(c['allowed_platforms']))}, "
            f"streamer {c['twitch_channel'] or '-'}, window {c['submission_window_min']}m")


def build_parser():
    p = argparse.ArgumentParser(prog="campaign_cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.environ.get("DB_PATH", "/data/pipeline.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, adding):
        d = (lambda v: v) if adding else (lambda v: None)
        sp.add_argument("--cpm", type=float, default=d(0.0))
        sp.add_argument("--flat-fee", type=float, default=d(0.0), dest="flat_fee")
        sp.add_argument("--min-payout", type=float, default=d(0.0), dest="min_payout")
        sp.add_argument("--cap", type=float, default=d(200.0), help="max payout per clip, USD")
        sp.add_argument("--budget", type=float, default=d(1000.0), help="total campaign budget, USD")
        sp.add_argument("--fee-rate", type=float, default=None, dest="fee_rate",
                        help="override creator fee (default: 10%%, 0%% for per_post/retainer >= $5000)")
        sp.add_argument("--window-min", type=int, default=d(30), dest="window_min")
        sp.add_argument("--earning-days", type=int, default=d(30), dest="earning_days")
        sp.add_argument("--platforms", default=d("tiktok,instagram,youtube"))
        sp.add_argument("--tags", default=None, help="required hashtags, e.g. '#gta6 #gtavi'")
        sp.add_argument("--disclosure", default=None)
        sp.add_argument("--source", choices=SOURCES, default=d("content_rewards"))
        sp.add_argument("--streamer", default=None, help="twitch channel or streamer id")

    a = sub.add_parser("add")
    a.add_argument("--name", required=True)
    a.add_argument("--model", choices=MODELS, required=True)
    common(a, True)
    e = sub.add_parser("edit")
    e.add_argument("id", type=int)
    e.add_argument("--name")
    e.add_argument("--model", choices=MODELS)
    common(e, False)
    for n in ("pause", "resume", "show"):
        sub.add_parser(n).add_argument("id", type=int)
    sub.add_parser("list").add_argument("--status", choices=STATUSES)
    ln = sub.add_parser("link")
    ln.add_argument("id", type=int)
    ln.add_argument("streamer")
    return p


def main(argv=None, db=None) -> int:
    args = build_parser().parse_args(argv)
    own = db is None
    db = db or State(args.db)
    try:
        if args.cmd == "add":
            cid = add_campaign(db, name=args.name, model=args.model, cpm=args.cpm, flat_fee=args.flat_fee,
                               min_payout=args.min_payout, cap=args.cap, budget=args.budget,
                               fee_rate=args.fee_rate, window_min=args.window_min, earning_days=args.earning_days,
                               platforms=args.platforms, tags=args.tags, disclosure=args.disclosure,
                               source=args.source, streamer=args.streamer)
            print("created", _fmt(get_campaign(db, cid)))
        elif args.cmd == "edit":
            ch = {k: v for k, v in vars(args).items() if k not in ("db", "cmd", "id")}
            edit_campaign(db, args.id, **ch)
            print("updated", _fmt(get_campaign(db, args.id)))
        elif args.cmd in ("pause", "resume"):
            set_status(db, args.id, "paused" if args.cmd == "pause" else "active")
            print(_fmt(get_campaign(db, args.id)))
        elif args.cmd == "link":
            edit_campaign(db, args.id, streamer=args.streamer)
            print(_fmt(get_campaign(db, args.id)))
        elif args.cmd == "show":
            print(json.dumps(get_campaign(db, args.id), indent=2))
        elif args.cmd == "list":
            q, par = "SELECT id FROM campaigns", ()
            if args.status:
                q, par = q + " WHERE status=?", (args.status,)
            for r in db.query(q + " ORDER BY id", par):
                print(_fmt(get_campaign(db, r["id"])))
        return 0
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
