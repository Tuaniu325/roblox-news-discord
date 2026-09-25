mport asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

USERNAME = "LeaksEvents"
PROFILE_URL = f"https://x.com/{USERNAME}"
SEEN_FILE = Path("data/seen.json")


def load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(
        json.dumps(sorted(seen)[-3000:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def truncate(value: str, limit: int = 2048) -> str:
    return value if len(value) <= limit else value[:limit - 3].rstrip() + "..."


def parse_dt(value: str):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def abs_url(url: str) -> str:
    return urljoin("https://x.com/", url)


def extract_from_article(article_html: str) -> dict | None:
    status_match = re.search(
        r'href=["\']([^"\']*/status/\d+[^"\']*)["\']',
        article_html,
        flags=re.I,
    )
    if not status_match:
        return None

    status_url = abs_url(status_match.group(1))
    id_match = re.search(r"/status/(\d+)", status_url)
    if not id_match:
        return None

    post_id = id_match.group(1)

    time_match = re.search(
        r'<time[^>]+datetime=["\']([^"\']+)["\']',
        article_html,
        flags=re.I,
    )
    created = parse_dt(time_match.group(1)) if time_match else None

    # X commonly keeps the visible post text in div[dir=auto].
    text_candidates = re.findall(
        r'<div[^>]+dir=["\']auto["\'][^>]*>(.*?)</div>',
        article_html,
        flags=re.I | re.S,
    )
    cleaned = [clean_text(x) for x in text_candidates]
    cleaned = [x for x in cleaned if x and len(x) > 1]

    # Prefer the longest reasonable text block.
    text = max(cleaned, key=len) if cleaned else ""

    if not text:
        # Fallback: remove tags from the whole article and keep a compact section.
        text = clean_text(article_html)

    # Skip obvious non-post UI fragments.
    if text.lower() in {"follow", "following", "more"}:
        return None

    images = re.findall(
        r'https://pbs\.twimg\.com/[^"\']+',
        article_html,
        flags=re.I,
    )
    image = images[0] if images else None

    return {
        "id": post_id,
        "url": status_url,
        "text": text,
        "created": created,
        "image": image,
    }


def extract_posts(page_html: str) -> list[dict]:
    # Public X profile pages generally contain tweet/article cards.
    articles = re.findall(
        r"<article\b.*?</article>",
        page_html,
        flags=re.I | re.S,
    )

    posts = []
    for article in articles:
        item = extract_from_article(article)
        if item:
            posts.append(item)

    # Deduplicate by post ID.
    posts = list({p["id"]: p for p in posts}.values())
    posts.sort(
        key=lambda p: p["created"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return posts


async def fetch_profile(client: httpx.AsyncClient) -> list[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    response = await client.get(
        PROFILE_URL,
        headers=headers,
        follow_redirects=True,
    )

    if response.status_code == 429:
        retry_after = response.headers.get("retry-after", "unknown")
        raise RuntimeError(f"X respondeu 429; retry-after={retry_after}")

    response.raise_for_status()

    posts = extract_posts(response.text)
    if not posts:
        raise RuntimeError(
            "X respondeu, mas nenhum post público foi encontrado no HTML. "
            "A estrutura da página pode ter mudado ou X pode exigir login."
        )

    return posts


async def send_discord(
    client: httpx.AsyncClient,
    webhook: str,
    post: dict,
) -> None:
    embed = {
        "title": "🔎 Roblox Events Leaks",
        "url": post["url"],
        "description": truncate(post["text"]),
        "color": 0xF0C419,
        "author": {
            "name": "@LeaksEvents",
            "url": PROFILE_URL,
        },
        "footer": {
            "text": "Roblox News • Fonte: X",
        },
    }

    if post["created"]:
        embed["timestamp"] = post["created"].astimezone(
            timezone.utc
        ).isoformat()

    if post.get("image"):
        embed["image"] = {"url": post["image"]}

    payload = {
        "username": os.getenv("DISCORD_WEBHOOK_NAME", "Roblox News"),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    response = await client.post(
        webhook,
        params={"wait": "true"},
        json=payload,
    )

    if response.status_code == 429:
        try:
            retry = float(response.json().get("retry_after", 2))
        except Exception:
            retry = 2
        await asyncio.sleep(retry)
        response = await client.post(
            webhook,
            params={"wait": "true"},
            json=payload,
        )

    response.raise_for_status()


async def main() -> None:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("Falta DISCORD_WEBHOOK_URL.")

    # In GitHub Actions this workflow runs once and exits.
    # Locally, LOOP=true can keep it alive.
    loop = os.getenv("LOOP", "false").lower() == "true"
    interval = max(300, int(os.getenv("CHECK_INTERVAL_SECONDS", "900")))
    max_posts = max(1, int(os.getenv("POSTS_PER_SOURCE", "10")))
    post_existing = (
        os.getenv("POST_EXISTING_ON_FIRST_RUN", "false").lower() == "true"
    )

    seen = load_seen()
    first_run = not bool(seen)

    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                posts = await fetch_profile(client)
                logging.info("@%s: %d posts encontrados.", USERNAME, len(posts))
            except Exception as exc:
                logging.error("Falha consultando @%s: %s", USERNAME, exc)
                posts = []

            posts = posts[:max_posts]
            new_posts = [p for p in posts if p["id"] not in seen]

            if first_run and not post_existing:
                for post in posts:
                    seen.add(post["id"])
                save_seen(seen)
                logging.info(
                    "Primeira execução: %d posts marcados como vistos.",
                    len(posts),
                )
            else:
                new_posts.sort(
                    key=lambda p: p["created"]
                    or datetime.min.replace(tzinfo=timezone.utc)
                )

                for post in new_posts:
                    try:
                        await send_discord(client, webhook, post)
                        seen.add(post["id"])
                        save_seen(seen)
                        logging.info("Publicado: %s", post["id"])
                    except Exception:
                        logging.exception(
                            "Não consegui enviar o post %s.",
                            post["id"],
                        )

            first_run = False

            if not loop:
                break

            logging.info("Aguardando %ss...", interval)
            await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
