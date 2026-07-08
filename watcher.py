from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from uniqlo_sales_alerter.config import AppConfig
from uniqlo_sales_alerter.services.sale_checker import SaleChecker

SETTINGS_PATH = Path("settings.json")
STATE_PATH = Path("data/state.json")
UPSTREAM_STATE_PATH = Path("/tmp/catalog-pulse-upstream-state.json")
TARGET_SIZES = {"S", "M", "L"}


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return loaded


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def euro(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " €"


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def contains_term(name: str, term: str) -> bool:
    """Case-insensitive matching, with word boundaries for short terms."""
    if len(term) <= 3 and term.isalpha():
        return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", name, re.IGNORECASE))
    return term.casefold() in name.casefold()


def category_for(name: str, settings: dict[str, Any]) -> str | None:
    lowered = name.casefold()
    keywords = settings["category_keywords"]

    # Order matters: "Hemdjacke" must be a jacket, not a shirt.
    for category in ("jackets", "pants", "tops"):
        if any(keyword.casefold() in lowered for keyword in keywords[category]):
            return category
    return None


def is_tshirt(name: str) -> bool:
    lowered = name.casefold()
    return "t-shirt" in lowered or "tee" in lowered


def exclusion_reason(
    name: str,
    category: str | None,
    settings: dict[str, Any],
) -> str | None:
    for term in settings["excluded_general"]:
        if contains_term(name, term):
            return f"excluded term: {term}"

    if category == "pants":
        for term in settings["excluded_pants"]:
            if contains_term(name, term):
                return f"excluded pants term: {term}"

    if category == "tops" and is_tshirt(name):
        for term in settings["excluded_tshirts"]:
            if contains_term(name, term):
                return f"excluded T-shirt term: {term}"

    return None


def item_to_record(item: Any, settings: dict[str, Any]) -> dict[str, Any] | None:
    name = normalize_name(item.name)
    category = category_for(name, settings)
    if category is None:
        return None

    if exclusion_reason(name, category, settings):
        return None

    price = float(item.sale_price)
    limit = float(settings["price_limits_eur"][category])
    if price > limit:
        return None

    sizes = sorted(
        {str(size).upper() for size in item.available_sizes} & TARGET_SIZES,
        key=lambda size: ("S", "M", "L").index(size),
    )
    if not sizes:
        return None

    urls = [url for url in item.product_urls if url]
    url = urls[0] if urls else (
        f"https://www.uniqlo.com/de/de/products/"
        f"{item.product_id}/{item.price_group or '00'}"
    )

    return {
        "product_id": item.product_id,
        "name": name,
        "category": category,
        "price": round(price, 2),
        "original_price": round(float(item.original_price), 2),
        "discount": round(float(item.discount_percentage), 1),
        "sizes": sizes,
        "url": url,
        "image_url": item.image_url or "",
        "active": True,
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }


def detect_events(
    current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for product_id, item in current.items():
        old = previous.get(product_id)

        if old is None:
            events.append({"type": "new", "item": item})
            continue

        if not old.get("active", False):
            events.append({"type": "restocked", "item": item})
            continue

        reasons: list[str] = []
        old_price = float(old.get("price", item["price"]))
        if item["price"] < old_price:
            reasons.append(f"Preis gefallen: {euro(old_price)} → {euro(item['price'])}")

        old_sizes = set(old.get("sizes", []))
        new_sizes = [size for size in item["sizes"] if size not in old_sizes]
        if new_sizes:
            reasons.append("Neu verfügbar: " + ", ".join(new_sizes))

        if reasons:
            events.append({"type": "changed", "item": item, "reasons": reasons})

    return events


def update_state(
    state: dict[str, Any],
    current: dict[str, dict[str, Any]],
    scan_count: int,
) -> dict[str, Any]:
    previous = state.get("products", {})
    merged = dict(previous)

    for product_id, old in list(merged.items()):
        if product_id not in current:
            old = dict(old)
            old["active"] = False
            merged[product_id] = old

    merged.update(current)

    cutoff = datetime.now(timezone.utc) - timedelta(days=120)
    pruned: dict[str, dict[str, Any]] = {}
    for product_id, record in merged.items():
        last_seen_raw = record.get("last_seen")
        try:
            last_seen = datetime.fromisoformat(last_seen_raw)
        except (TypeError, ValueError):
            last_seen = datetime.now(timezone.utc)
        if record.get("active", False) or last_seen >= cutoff:
            pruned[product_id] = record

    state["products"] = pruned
    state["last_scan_count"] = scan_count
    state["last_success_date"] = datetime.now(timezone.utc).date().isoformat()
    state["last_error"] = {}
    return state


def discord_post(payload: dict[str, Any]) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("GitHub Secret DISCORD_WEBHOOK_URL is missing")

    response = requests.post(webhook, json=payload, timeout=20)
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord webhook returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )


def event_embed(event: dict[str, Any]) -> dict[str, Any]:
    item = event["item"]
    event_type = event["type"]

    titles = {
        "new": "Neuer passender Artikel",
        "restocked": "Wieder verfügbar",
        "changed": "Wesentliche Änderung",
    }
    colors = {
        "new": 0x3498DB,
        "restocked": 0xF39C12,
        "changed": 0x2ECC71,
    }
    icons = {
        "tops": "👕",
        "pants": "👖",
        "jackets": "🧥",
    }

    details = event.get("reasons", [])
    detail_text = "\n".join(f"• {reason}" for reason in details)
    if detail_text:
        detail_text += "\n\n"

    price_line = f"**{euro(item['price'])}**"
    if item["original_price"] > item["price"]:
        price_line = (
            f"~~{euro(item['original_price'])}~~ → "
            f"**{euro(item['price'])}** (-{item['discount']:.0f} %)"
        )

    embed: dict[str, Any] = {
        "title": f"{icons[item['category']]} {item['name']}"[:256],
        "url": item["url"],
        "description": (
            f"**{titles[event_type]}**\n\n"
            f"{detail_text}"
            f"💶 {price_line}\n"
            f"📏 Größen: **{', '.join(item['sizes'])}**\n"
            f"🔗 [Direkt zum Artikel]({item['url']})"
        ),
        "color": colors[event_type],
        "footer": {"text": "Catalog Pulse"},
    }
    if item.get("image_url"):
        embed["thumbnail"] = {"url": item["image_url"]}
    return embed


def send_events(events: list[dict[str, Any]]) -> None:
    for start in range(0, len(events), 10):
        chunk = events[start : start + 10]
        discord_post({
            "username": "Catalog Pulse",
            "content": "🚨 Neue passende UNIQLO-Änderung" if start == 0 else "",
            "embeds": [event_embed(event) for event in chunk],
        })


def send_baseline_message(scan_count: int, match_count: int) -> None:
    discord_post({
        "username": "Catalog Pulse",
        "embeds": [{
            "title": "✅ Überwachung ist aktiv",
            "description": (
                f"Die erste Prüfung war erfolgreich.\n\n"
                f"• Sale-Produkte geprüft: **{scan_count}**\n"
                f"• Passend zu deinen Filtern: **{match_count}**\n\n"
                "Diese Treffer wurden nur als Ausgangslage gespeichert. "
                "Ab jetzt kommen ausschließlich neue Artikel, Preisfälle "
                "oder neu verfügbare Größen S/M/L."
            ),
            "color": 0x2ECC71,
            "footer": {"text": "Catalog Pulse"},
        }],
    })


def send_error_once(state: dict[str, Any], message: str) -> None:
    now = datetime.now(timezone.utc)
    last_error = state.get("last_error", {})
    same_error = last_error.get("message") == message
    try:
        last_sent = datetime.fromisoformat(last_error.get("sent_at", ""))
    except (TypeError, ValueError):
        last_sent = datetime.min.replace(tzinfo=timezone.utc)

    if same_error and now - last_sent < timedelta(hours=6):
        return

    discord_post({
        "username": "Catalog Pulse",
        "embeds": [{
            "title": "⚠️ Prüfung fehlgeschlagen",
            "description": (
                "Die Produktdaten konnten nicht zuverlässig geprüft werden. "
                "Der bisherige Zustand wurde **nicht überschrieben**.\n\n"
                f"`{message[:900]}`"
            ),
            "color": 0xE74C3C,
            "footer": {"text": "Catalog Pulse"},
        }],
    })
    state["last_error"] = {
        "message": message,
        "sent_at": now.isoformat(),
    }
    save_json(STATE_PATH, state)


def build_upstream_config(settings: dict[str, Any]) -> AppConfig:
    return AppConfig.model_validate({
        "uniqlo": {
            "country": "de/de",
            "check_interval_minutes": 0,
        },
        "filters": {
            "gender": ["MEN"],
            "min_sale_percentage": 0,
            "sizes": {
                "clothing": settings["sizes"],
                "pants": [],
                "shoes": [],
                "one_size": False,
            },
            "ignored_keywords": [],
        },
        "notifications": {
            "notify_on": "every_check",
            "check_on_startup": False,
            "low_stock_threshold": 0,
            "suppress_low_stock_alerts": False,
            "alert_reasons": ["new", "new_variant", "restocked", "price_drop"],
        },
    })


async def fetch_current(settings: dict[str, Any]) -> tuple[int, dict[str, dict[str, Any]]]:
    config = build_upstream_config(settings)
    checker = SaleChecker(config, state_file=UPSTREAM_STATE_PATH)
    try:
        result = await checker.check()
    finally:
        await checker.close()

    current: dict[str, dict[str, Any]] = {}
    for item in result.matching_deals:
        record = item_to_record(item, settings)
        if record is not None:
            current[record["product_id"]] = record

    return int(result.total_products_scanned), current


def validate_scan(scan_count: int, state: dict[str, Any], settings: dict[str, Any]) -> None:
    minimum = int(settings["health"]["minimum_first_scan"])
    previous = int(state.get("last_scan_count", 0) or 0)

    if previous <= 0 and scan_count < minimum:
        raise RuntimeError(
            f"API returned only {scan_count} sale products; expected at least {minimum}"
        )

    if previous > 0:
        allowed_floor = max(
            minimum,
            int(previous * float(settings["health"]["minimum_previous_ratio"])),
        )
        if scan_count < allowed_floor:
            raise RuntimeError(
                f"Sale product count dropped unexpectedly from {previous} "
                f"to {scan_count} (safety floor: {allowed_floor})"
            )


async def main_async() -> None:
    settings = load_json(SETTINGS_PATH, {})
    state = load_json(
        STATE_PATH,
        {"version": 1, "initialized": False, "products": {}, "last_error": {}},
    )

    try:
        scan_count, current = await fetch_current(settings)
        validate_scan(scan_count, state, settings)

        if not state.get("initialized", False):
            state["initialized"] = True
            state = update_state(state, current, scan_count)
            save_json(STATE_PATH, state)
            send_baseline_message(scan_count, len(current))
            print(
                f"Baseline created: scanned={scan_count}, matches={len(current)}"
            )
            return

        events = detect_events(current, state.get("products", {}))
        if events:
            send_events(events)
            print(f"Sent {len(events)} Discord alert(s)")
        else:
            print(
                f"No new relevant changes: scanned={scan_count}, "
                f"matches={len(current)}"
            )

        state = update_state(state, current, scan_count)
        save_json(STATE_PATH, state)

    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)
        try:
            send_error_once(state, message)
        except Exception as notify_exc:
            print(
                f"Could not send Discord error notification: {notify_exc}",
                file=sys.stderr,
            )
        raise


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
