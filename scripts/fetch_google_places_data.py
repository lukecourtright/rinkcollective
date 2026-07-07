"""One-time backfill: match rinks to Google Places, then pull real ratings/
reviews/photos into rinks.json. Two phases mirror the existing CSV workflow
(export_rinks_csv.py / merge_rinks_csv.py) so a human can catch bad
fuzzy-matches before they land in rinks.json:

  1. match  - text-search each rink by name+address, write candidate
              place_id matches to a review CSV (no rinks.json writes)
  2. apply  - after hand-editing that CSV (blank out bad matches), pull
              Place Details for each remaining match and merge real
              rating/reviewCount/reviews/photos/googlePlaceId into
              rinks.json by id

This is a one-time pull, not an ongoing sync — rinks that already have a
googlePlaceId are skipped by `match` on subsequent runs. Photo images
themselves are not downloaded here; only each photo's Google resource
name is recorded, fetched later on demand by the /api/photos proxy in
main.py.

Requires GOOGLE_PLACES_API_KEY in the environment and Places API (New)
enabled on the associated Google Cloud project.

Usage:
  python scripts/fetch_google_places_data.py match [--limit N] [--out FILE]
  python scripts/fetch_google_places_data.py apply <csv_path> [--limit N]
"""
import argparse
import csv
import json
import os
import pathlib
import sys
import time

import httpx

RINKS_FILE = pathlib.Path(__file__).resolve().parent.parent / "rinks.json"
API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
MAX_PHOTOS = 8
MATCH_FIELDNAMES = ["id", "name", "address", "city", "state", "matched_place_id", "matched_name", "matched_address"]


def require_api_key():
    if not API_KEY:
        sys.exit("GOOGLE_PLACES_API_KEY is not set")


def search_text(client, query):
    resp = client.post(
        "https://places.googleapis.com/v1/places:searchText",
        json={"textQuery": query},
        headers={
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
        },
    )
    resp.raise_for_status()
    places = resp.json().get("places", [])
    return places[0] if places else None


def get_details(client, place_id):
    resp = client.get(
        f"https://places.googleapis.com/v1/places/{place_id}",
        headers={"X-Goog-Api-Key": API_KEY, "X-Goog-FieldMask": "rating,userRatingCount,reviews,photos"},
    )
    resp.raise_for_status()
    return resp.json()


def cmd_match(args):
    require_api_key()
    rinks = json.loads(RINKS_FILE.read_text(encoding="utf-8"))
    pending = [r for r in rinks if not r.get("googlePlaceId")]
    if args.limit:
        pending = pending[: args.limit]

    rows, matched, unmatched = [], 0, 0
    with httpx.Client(timeout=15) as client:
        for r in pending:
            query = f"{r['name']} {r['address']} {r['city']} {r['state']}"
            try:
                place = search_text(client, query)
            except httpx.HTTPStatusError as e:
                print(f"  error on id {r['id']} ({r['name']}): {e}")
                place = None
            rows.append({
                "id": r["id"], "name": r["name"], "address": r["address"],
                "city": r["city"], "state": r["state"],
                "matched_place_id": place["id"] if place else "",
                "matched_name": place.get("displayName", {}).get("text", "") if place else "",
                "matched_address": place.get("formattedAddress", "") if place else "",
            })
            matched += bool(place)
            unmatched += not place
            time.sleep(0.05)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Matched {matched}, unmatched {unmatched}. Review {args.out} (blank out bad matches) before running apply.")


def cmd_apply(args):
    require_api_key()
    rinks = json.loads(RINKS_FILE.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in rinks}

    with open(args.csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[: args.limit]

    applied, skipped, errors = 0, 0, 0
    with httpx.Client(timeout=15) as client:
        for row in rows:
            place_id = row.get("matched_place_id", "").strip()
            if not place_id:
                skipped += 1
                continue
            rink = by_id.get(int(row["id"]))
            if rink is None:
                print(f"  id {row['id']} not found in rinks.json, skipping")
                errors += 1
                continue
            try:
                details = get_details(client, place_id)
            except httpx.HTTPStatusError as e:
                print(f"  error fetching details for id {rink['id']} ({rink['name']}): {e}")
                errors += 1
                continue

            changes = []
            if "rating" in details and details["rating"] != rink.get("rating"):
                rink["rating"] = round(details["rating"], 1)
                changes.append("rating")
            if "userRatingCount" in details and details["userRatingCount"] != rink.get("reviewCount"):
                rink["reviewCount"] = details["userRatingCount"]
                changes.append("reviewCount")

            google_reviews = [
                {
                    "author": rv.get("authorAttribution", {}).get("displayName", "Google user"),
                    "rating": rv.get("rating", 0),
                    "text": rv.get("text", {}).get("text", ""),
                    "date": rv.get("relativePublishTimeDescription", ""),
                    "source": "google",
                }
                for rv in details.get("reviews", [])
            ]
            curated = [rv for rv in rink.get("reviews", []) if rv.get("source") != "google"]
            if google_reviews:
                rink["reviews"] = google_reviews + curated
                changes.append("reviews")

            photos = [
                {
                    "ref": p["name"],
                    "attribution": (p.get("authorAttributions") or [{}])[0].get("displayName", "Google user"),
                }
                for p in details.get("photos", [])[:MAX_PHOTOS]
            ]
            if photos:
                rink["photos"] = photos
                changes.append("photos")

            rink["googlePlaceId"] = place_id
            changes.append("googlePlaceId")

            print(f"  id {rink['id']} ({rink['name']}): updated {', '.join(changes)}")
            applied += 1
            time.sleep(0.05)

    RINKS_FILE.write_text(json.dumps(rinks, indent=2), encoding="utf-8")
    print(f"\n{applied} applied, {skipped} skipped (no match), {errors} errors")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_match = sub.add_parser("match", help="Search Google Places for each unmatched rink, write a review CSV")
    p_match.add_argument("--limit", type=int, default=None)
    p_match.add_argument("--out", default="rinks_google_match_review.csv")
    p_match.set_defaults(func=cmd_match)

    p_apply = sub.add_parser("apply", help="Pull Place Details for each matched rink and merge into rinks.json")
    p_apply.add_argument("csv_path")
    p_apply.add_argument("--limit", type=int, default=None)
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
