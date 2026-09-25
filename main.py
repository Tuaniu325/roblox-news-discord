import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

DATA_DIR = Path("data")
SEEN_FILE = DATA_DIR / "seen.json"

SYNDICATION_URL = (
    "https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
)

SOURCES = [
    {"username": "LeaksEvents", "label": "Roblox Events Leaks", "emoji": "🔎", "color": 0xF0C419},
    {"username": "Bloxy_News", "label": "Bloxy News", "emoji": "📰", "color": 0x3498DB},
]


def load_seen() -> set[str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the repository file small.
    items = sorted(seen)[-3000:]
    SEEN_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_text(value: str) -> str:
    value = html.unescape(value or "").strip()
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value


def truncate(value: str, limit: int = 2048) -> str:
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value)
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_profile(html_text: str, username: str) -> list[dict[str, Any]]:
    marker = '<script id="__NEXT_DATA__" type="application/json">'
    start = html_text.find(marker)
    if start < 0:
        raise RuntimeError("Feed do X não trouxe __NEXT_DATA__.")
    start += len(marker)
    end = html_text.find("</script>", start)
    if end < 0:
        raise RuntimeError("Não achei o fim de __NEXT_DATA__.")

    data = json.loads(html_text[start:end])
    entries = (
        data.get("props", {})
        .get("pageProps", {})
        .get("timeline", {})
        .get("entries", [])
    )

    result = []
    for entry in entries:
        tweet = entry.get("content", {}).get("tweet", {})
        if not isinstance(tweet, dict):
            continue

        tweet_id = str(tweet.get("id_str") or tweet.get("id") or "").strip()
        if not tweet_id:
            continue

        user = tweet.get("user") or {}
        screen_name = str(user.get("screen_name") or "").strip()

        # Ignore reposts/retweets from other accounts.
        if screen_name and screen_name.lower() != username.lower():
            continue

        text = clean_text(str(tweet.get("full_text") or tweet.get("text") or ""))
        if not text or text.startswith("RT @"):
            continue

        media = []
        for item in (tweet.get("entities") or {}).get("media", []) or []:
            if isinstance(item, dict):
                media.append(item)

        created_at = parse_date(tweet.get("created_at"))

        result.append(
            {
                "id": tweet_id,
                "text": text,
                "created_at": created_at,
                "permalink": tweet.get("permalink")
                or f"https://x.com/{username}/status/{tweet_id}",
                "user": user,
                "media": media,
                "likes": int(tweet.get("favorite_count") or 0),
                "reposts": int(tweet.get("retweet_count") or 0),
                "replies": int(tweet.get("reply_count") or 0),
            }
        )

    unique = {item["id"]: item for item in result}
    result = list(unique.values())
    result.sort(
        key=lambda item: item["created_at"]
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return result


def choose_image(tweet: dict[str, Any]) -> str | None:
    for item in tweet.get("media", []):
        for key in ("media_url_https", "media_url", "url"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                if "pbs.twimg.com" in value:
                    return value
    return None


async def fetch_source(
    client: httpx.AsyncClient,
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    url = SYNDICATION_URL.format(username=source["username"])
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for attempt in range(2):
        response = await client.get(url, headers=headers, follow_redirects=True)
        if response.status_code == 429 and attempt == 0:
            retry_after = response.headers.get("retry-after")
            try:
                delay = min(max(float(retry_after), 30), 300)
            except (TypeError, ValueError):
                delay = 60
            logging.warning("@%s recebeu 429; esperando %.0fs.", source["username"], delay)
            await asyncio.sleep(delay)
            continue

        response.raise_for_status()
        return parse_profile(response.text, source["username"])

    return []


async def send_discord(
    client: httpx.AsyncClient,
    webhook: str,
    source: dict[str, Any],
    tweet: dict[str, Any],
) -> None:
    created_at = tweet.get("created_at")
    embed: dict[str, Any] = {
        "title": f'{source["emoji"]} {source["label"]}',
        "url": tweet["permalink"],
        "description": truncate(tweet["text"]),
        "color": source["color"],
        "author": {
            "name": f'{tweet["user"].get("name", source["label"])} (@{source["username"]})',
            "url": f'https://x.com/{source["username"]}',
        },
        "footer": {"text": "Roblox News • Fonte: X"},
        "fields": [
            {"name": "❤️ Curtidas", "value": str(tweet["likes"]), "inline": True},
            {"name": "🔁 Reposts", "value": str(tweet["reposts"]), "inline": True},
            {"name": "💬 Respostas", "value": str(tweet["replies"]), "inline": True},
        ],
    }

    if created_at:
        embed["timestamp"] = created_at.astimezone(timezone.utc).isoformat()

    profile_image = tweet["user"].get("profile_image_url_https")
    if profile_image:
        embed["author"]["icon_url"] = profile_image

    image = choose_image(tweet)
    if image:
        embed["image"] = {"url": image}

    payload = {
        "username": os.getenv("DISCORD_WEBHOOK_NAME", "Roblox News"),
        "avatar_url": os.getenv("DISCORD_WEBHOOK_AVATAR", ""),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    response = await client.post(webhook, params={"wait": "true"}, json=payload)
    if response.status_code == 429:
        try:
            body = response.json()
            delay = float(body.get("retry_after", 2))
        except Exception:
            delay = 2
        await asyncio.sleep(delay)
        response = await client.post(webhook, params={"wait": "true"}, json=payload)

    response.raise_for_status()


async def main() -> None:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("Falta DISCORD_WEBHOOK_URL.")

    seen = load_seen()
    first_run = not bool(seen)
    post_existing = os.getenv("POST_EXISTING_ON_FIRST_RUN", "false").lower() == "true"
    posts_per_source = max(1, int(os.getenv("POSTS_PER_SOURCE", "10")))

    async with httpx.AsyncClient(timeout=40) as client:
        for source in SOURCES:
            try:
                tweets = await fetch_source(client, source)
                logging.info("@%s: %d posts no feed.", source["username"], len(tweets))
            except Exception:
                logging.exception("Falha ao consultar @%s.", source["username"])
                continue

            tweets = tweets[:posts_per_source]
            new_tweets = [tweet for tweet in tweets if tweet["id"] not in seen]

            if first_run and not post_existing:
                for tweet in tweets:
                    seen.add(tweet["id"])
                continue

            new_tweets.sort(
                key=lambda item: item["created_at"]
                or datetime.min.replace(tzinfo=timezone.utc)
            )

            for tweet in new_tweets:
                try:
                    await send_discord(client, webhook, source, tweet)
                    seen.add(tweet["id"])
                    save_seen(seen)
                    logging.info(
                        "Publicado @%s: %s",
                        source["username"],
                        tweet["id"],
                    )
                except Exception:
                    logging.exception(
                        "Falha ao publicar @%s: %s",
                        source["username"],
                        tweet["id"],
                    )

            # Pequena pausa entre fontes para reduzir bursts.
            await asyncio.sleep(10)

    save_seen(seen)


if __name__ == "__main__":
    asyncio.run(main())
