import asyncio, html, json, logging, os, re, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
SEEN = Path("data/seen.json")
BASE = os.getenv("RSSHUB_BASE", "https://rsshub.app").rstrip("/")
SOURCES = [
    ("LeaksEvents", "Roblox Events Leaks", "🔎", 0xF0C419),
    ("Bloxy_News", "Bloxy News", "📰", 0x3498DB),
]

def load_seen():
    try:
        return set(json.loads(SEEN.read_text(encoding="utf-8")))
    except Exception:
        return set()

def save_seen(s):
    SEEN.parent.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps(sorted(s)[-3000:], ensure_ascii=False, indent=2), encoding="utf-8")

def lname(tag):
    return tag.split("}", 1)[-1].lower()

def text_of(e, names):
    for c in list(e):
        if lname(c.tag) in names:
            return (c.text or "").strip()
    return ""

def clean(s):
    s = html.unescape(s or "")
    s = re.sub(r"<br\\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()

def date_of(s):
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def parse_feed(xml_text, source):
    root = ET.fromstring(xml_text)
    out = []
    for e in root.iter():
        if lname(e.tag) not in {"item", "entry"}:
            continue
        title = text_of(e, {"title"})
        desc = text_of(e, {"description", "summary", "content"})
        guid = text_of(e, {"guid", "id"})
        pub = text_of(e, {"pubdate", "published", "updated"})
        link = guid
        for c in list(e):
            if lname(c.tag) == "link":
                link = c.attrib.get("href") or (c.text or "").strip() or link
                break
        if not (link or guid):
            continue

        image = None
        for c in e.iter():
            if lname(c.tag) in {"content", "thumbnail", "enclosure", "image"}:
                image = c.attrib.get("url") or c.attrib.get("href")
                if image:
                    break
        if not image:
            m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)', desc, re.I)
            image = html.unescape(m.group(1)) if m else None

        body = clean(desc)
        title = clean(title)
        full = title if not body or body.lower() == title.lower() else f"{title}\n\n{body}"
        ident = guid or link
        out.append({"id": ident, "text": full[:2048], "url": link, "image": image, "created": date_of(pub)})

    d = {x["id"]: x for x in out}
    return sorted(d.values(), key=lambda x: x["created"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

async def fetch(client, username):
    url = f"{BASE}/twitter/user/{username}"
    r = await client.get(url, headers={"User-Agent":"Roblox-News-Discord/1.0","Accept":"application/rss+xml, application/xml, text/xml, */*"}, follow_redirects=True)
    if r.status_code == 429:
        raise RuntimeError(f"RSSHub 429 para @{username}")
    r.raise_for_status()
    return r.text

async def send(client, webhook, source, item):
    username, label, emoji, color = source
    embed = {
        "title": f"{emoji} {label}", "url": item["url"], "description": item["text"],
        "color": color, "footer": {"text": "Roblox News • Fonte: X"},
        "fields": [{"name":"Fonte","value":f"[@{username}](https://x.com/{username})","inline":True}]
    }
    if item["created"]:
        embed["timestamp"] = item["created"].astimezone(timezone.utc).isoformat()
    if item["image"]:
        embed["image"] = {"url": item["image"]}
    payload = {"username": os.getenv("DISCORD_WEBHOOK_NAME","Roblox News"), "embeds":[embed], "allowed_mentions":{"parse":[]}}
    r = await client.post(webhook, params={"wait":"true"}, json=payload)
    r.raise_for_status()

async def main():
    webhook = os.getenv("DISCORD_WEBHOOK_URL","").strip()
    if not webhook:
        raise SystemExit("Falta DISCORD_WEBHOOK_URL.")
    interval = max(300, int(os.getenv("CHECK_INTERVAL_SECONDS","900")))
    max_posts = max(1, int(os.getenv("POSTS_PER_SOURCE","10")))
    post_existing = os.getenv("POST_EXISTING_ON_FIRST_RUN","false").lower() == "true"
    seen = load_seen()
    first = not bool(seen)

    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            for source in SOURCES:
                try:
                    xml = await fetch(client, source[0])
                    items = parse_feed(xml, source)[:max_posts]
                    logging.info("@%s: %d entradas encontradas.", source[0], len(items))
                except Exception as e:
                    logging.error("@%s: %s", source[0], e)
                    continue

                new = [x for x in items if x["id"] not in seen]
                if first and not post_existing:
                    seen.update(x["id"] for x in items)
                    continue

                for item in reversed(new):
                    try:
                        await send(client, webhook, source, item)
                        seen.add(item["id"])
                        save_seen(seen)
                    except Exception:
                        logging.exception("Falha enviando @%s", source[0])
            save_seen(seen)
            first = False
            await asyncio.sleep(interval)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
