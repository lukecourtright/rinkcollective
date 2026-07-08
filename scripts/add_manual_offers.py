"""Manual entry path for real Equipment offers — for retailers/products not
(yet) covered by an automated network integration. Both automated paths are
currently blocked: AvantLink denied the site's affiliate application for
being too new/low-traffic (can reapply once it has more content/traffic),
and Amazon PA-API requires 10 sales in 30 days before granting API access.
See CLAUDE.md's Live Offers section.

A human looks up the real product on a retailer's site, copies the current
price and product page URL by hand, and this script writes it straight into
EquipmentOffer/EquipmentPriceSnapshot — no API call, and no affiliate tag
needed on the url itself. Once Sovrn Commerce (formerly VigLink) is
installed site-wide (see CLAUDE.md), it rewrites these plain outbound links
into tracked/monetized ones at click time automatically — no per-retailer
integration required on our end for that part.

CSV columns:
  id            - existing Equipment id to attach this offer to. Leave
                  blank to create a brand-new product using the four
                  columns below.
  category, brand, name, image - only used when id is blank.
  retailer_name - e.g. "Pure Hockey"
  price         - current price, plain number
  url           - the real product page URL (plain, no affiliate tag)

Re-running with the same id+retailer_name updates that offer's price/url in
place and logs a new EquipmentPriceSnapshot — this doubles as the "refresh"
step for manually-tracked offers until an automated network takes over.

Usage:
  python scripts/add_manual_offers.py <csv_path> [--limit N]
"""
import argparse
import csv
import json
import pathlib
import sys
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EQUIPMENT_FILE = REPO_ROOT / "equipment.json"
NETWORK = "manual"


def get_app_main():
    # main.py isn't a package; add the repo root to sys.path so the script
    # can import its engine/models regardless of the caller's cwd.
    sys.path.insert(0, str(REPO_ROOT))
    import main as app_main
    return app_main


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    app_main = get_app_main()
    from sqlmodel import Session, select

    products = json.loads(EQUIPMENT_FILE.read_text(encoding="utf-8"))

    with open(args.csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[: args.limit]

    applied, skipped, errors = 0, 0, 0
    touched_json = False
    with Session(app_main.engine) as session:
        for row in rows:
            retailer_name = row.get("retailer_name", "").strip()
            price_raw = row.get("price", "").strip()
            url = row.get("url", "").strip()
            if not retailer_name or not price_raw or not url:
                print(f"  skipping row (missing retailer_name/price/url): {row}")
                skipped += 1
                continue
            price = float(price_raw)

            id_raw = row.get("id", "").strip()
            if id_raw:
                equipment_id = int(id_raw)
                if session.get(app_main.Equipment, equipment_id) is None:
                    print(f"  id {equipment_id} not found in Equipment table, skipping")
                    errors += 1
                    continue
            else:
                equipment_id = max((p["id"] for p in products), default=100) + 1
                product = {
                    "id": equipment_id,
                    "category": row.get("category", ""),
                    "brand": row.get("brand", ""),
                    "name": row.get("name", ""),
                    "rating": 0,
                    "reviewCount": 0,
                    "imageUrl": row.get("image") or None,
                    "deal": None,
                    "note": "Stable price",
                    "priceIsGood": False,
                    "wasPrice": None,
                    "priceHistory": [],
                    "featuredQuote": "",
                    "retailers": [],
                    "specs": [],
                    "reviewList": [],
                }
                products.append(product)
                session.merge(app_main.Equipment(**product))
                touched_json = True

            existing_offer = session.exec(
                select(app_main.EquipmentOffer).where(
                    app_main.EquipmentOffer.equipmentId == equipment_id,
                    app_main.EquipmentOffer.network == NETWORK,
                    app_main.EquipmentOffer.retailerName == retailer_name,
                )
            ).first()

            if existing_offer is None:
                offer = app_main.EquipmentOffer(
                    equipmentId=equipment_id,
                    retailerName=retailer_name,
                    network=NETWORK,
                    sourceProductId=f"manual-{equipment_id}-{retailer_name}",
                    price=price,
                    url=url,
                    inStock=True,
                )
            else:
                existing_offer.price = price
                existing_offer.url = url
                existing_offer.lastCheckedAt = datetime.now(timezone.utc).isoformat()
                offer = existing_offer

            session.add(offer)
            session.commit()
            session.refresh(offer)
            session.add(app_main.EquipmentPriceSnapshot(equipmentOfferId=offer.id, price=price))
            session.commit()

            print(f"  {retailer_name} @ equipment id {equipment_id}: {'added' if existing_offer is None else 'updated'} ${price}")
            applied += 1

    if touched_json:
        EQUIPMENT_FILE.write_text(json.dumps(products, indent=2), encoding="utf-8")
        print("equipment.json updated with new curated entries — push to main to sync on next deploy.")

    print(f"\n{applied} applied, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
