# -*- coding: utf-8 -*-
"""
Vorax VALORANT News Digest Bot (VLR.gg + THESPIKE.GG) -> Discord Webhook
Versão: 4.0 (fix + embeds PT-BR + 2+2)

O que faz (por execução):
- Busca as notícias mais recentes:
  - VLR.gg via RSS oficial: https://www.vlr.gg/rss
  - THESPIKE.GG via listagem: https://www.thespike.gg/br/valorant/news (ou /valorant/news)
- Posta no Discord via webhook:
  - 2 notícias do VLR + 2 notícias do THESPIKE (balanceado, alternando)
- Anti-duplicado:
  - Guarda UIDs em posted_cache.json (30 dias por padrão)
- Embeds:
  - PT-BR (labels), com imagem da notícia (og:image) quando existir
  - Se o THESPIKE devolver título “Loading...”, usa og:title automaticamente.

Como configurar webhook (seguro):
- Recomendado: arquivo local "webhook_url.txt" na mesma pasta deste script, contendo a URL do webhook (1 linha)
- Alternativa: variável de ambiente DISCORD_WEBHOOK_URL

Dependências:
  pip install requests beautifulsoup4 lxml

Execução:
  python bot_noticias_digest_ptbr.py
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# =========================
# CONFIG
# =========================
PROJECT_DIR = Path(__file__).resolve().parent
WEBHOOK_FILE = PROJECT_DIR / "webhook_url.txt"

# Webhook: env > file
WEBHOOK_URL = os.getenv("https://discord.com/api/webhooks/1469139181954535638/BUqNbD-ZHjAvMeJEuU5-dlu2ROtU5UkQOMUfobfTK8E9XcKobRv9Aln9ffH0euRFa63X", "").strip()

# Brand / visual
EMBED_COLOR = int(os.getenv("EMBED_COLOR", "0"))  # #000000
FOOTER_TEXT = os.getenv("FOOTER_TEXT", "Vorax eSports • Notícias da Comunidade").strip()

# Use a URL SEM parâmetros (evita expirar)
VORAX_THUMB_URL = os.getenv(
    "https://cdn.discordapp.com/attachments/1440760869964484738/1469322383449002004/vorax_icon_alt_on-ghost.png.png?ex=69873c9a&is=6985eb1a&hm=3a8bcedb0ab88866c056197affea5b79a5cbd7a42d90b45a3940abb7862d3077&",
    "https://cdn.discordapp.com/attachments/1440760869964484738/1469322383449002004/vorax_icon_alt_on-ghost.png.png?ex=69873c9a&is=6985eb1a&hm=3a8bcedb0ab88866c056197affea5b79a5cbd7a42d90b45a3940abb7862d3077&",
).strip()
USE_VORAX_THUMBNAIL = os.getenv("USE_VORAX_THUMBNAIL", "1") != "0"

# Digest limits (2 + 2)
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "4"))
MAX_VLR_PER_RUN = int(os.getenv("MAX_VLR_PER_RUN", "2"))
MAX_THESPIKE_PER_RUN = int(os.getenv("MAX_THESPIKE_PER_RUN", "2"))

# Runtime
HTTP_TIMEOUT_SEC = int(os.getenv("HTTP_TIMEOUT_SEC", "20"))
SLEEP_BETWEEN_POSTS_SEC = float(os.getenv("SLEEP_BETWEEN_POSTS_SEC", "1.0"))

# Cache / anti-duplicate
CACHE_FILE = Path(os.getenv("CACHE_FILE", str(PROJECT_DIR / "posted_cache.json")))
CACHE_KEEP_DAYS = int(os.getenv("CACHE_KEEP_DAYS", "30"))

# Sources toggles
ENABLE_VLR = os.getenv("ENABLE_VLR", "1") != "0"
ENABLE_THESPIKE = os.getenv("ENABLE_THESPIKE", "1") != "0"
THESPIKE_LOCALE = os.getenv("THESPIKE_LOCALE", "br").strip().lower()  # "br" or "en"

# Translation (best-effort)
TRANSLATE_PT = os.getenv("TRANSLATE_PT", "1") != "0"
TRANSLATE_ONLY_VLR = os.getenv("TRANSLATE_ONLY_VLR", "1") != "0"  # default: translate only VLR titles/descriptions
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.de/translate").strip()
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
TRANSLATE_CACHE_FILE = Path(os.getenv("TRANSLATE_CACHE_FILE", str(PROJECT_DIR / "translate_cache.json")))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(message)s")
log = logging.getLogger("vorax-news-digest")

# HTTP session
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VoraxNewsDigest/4.0",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
)

# =========================
# DATA
# =========================
@dataclass(frozen=True)
class NewsItem:
    uid: str
    title: str
    url: str
    source: str
    description: str = ""
    published_ts: Optional[int] = None  # unix seconds


SOURCE_ICONS = {
    "VLR.gg": "https://www.google.com/s2/favicons?domain=vlr.gg&sz=64",
    "THESPIKE.GG": "https://www.google.com/s2/favicons?domain=thespike.gg&sz=64",
}


# =========================
# UTILS
# =========================
def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def clean_text(text: str) -> str:
    t = (text or "").strip()
    t = _html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "lxml")
    return clean_text(soup.get_text(" "))


def safe_get(url: str) -> str:
    r = SESSION.get(url, timeout=HTTP_TIMEOUT_SEC)
    r.raise_for_status()
    return r.text


def read_webhook_url() -> str:
    if WEBHOOK_URL:
        return WEBHOOK_URL
    if WEBHOOK_FILE.exists():
        return WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    return ""


# =========================
# CACHE
# =========================
def load_cache() -> dict[str, int]:
    if not CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): int(v) for k, v in raw.items()}
        if isinstance(raw, list):  # compat
            return {str(x): _now_ts() for x in raw}
    except Exception as e:
        log.warning(f"Cache inválido, ignorando: {e}")
    return {}


def save_cache(cache: dict[str, int]) -> None:
    cutoff = _now_ts() - (CACHE_KEEP_DAYS * 24 * 3600)
    pruned = {k: v for k, v in cache.items() if v >= cutoff}
    CACHE_FILE.write_text(json.dumps(pruned, ensure_ascii=False, indent=2), encoding="utf-8")


def load_translate_cache() -> dict[str, str]:
    if not TRANSLATE_CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(TRANSLATE_CACHE_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def save_translate_cache(cache: dict[str, str]) -> None:
    # keep it reasonably sized
    if len(cache) > 2000:
        keys = list(cache.keys())
        for k in keys[: len(cache) - 2000]:
            cache.pop(k, None)
    TRANSLATE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# =========================
# META EXTRACTION (og:image, og:title, og:description)
# =========================
def extract_meta(page_url: str) -> tuple[str, Optional[str], Optional[int], Optional[str]]:
    """
    Returns: (description, og_image_url, published_ts (best effort), og_title)
    """
    try:
        html_text = safe_get(page_url)
        soup = BeautifulSoup(html_text, "lxml")

        def _meta(prop: str = "", name: str = "") -> Optional[str]:
            tag = None
            if prop:
                tag = soup.find("meta", attrs={"property": prop})
            if not tag and name:
                tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
            return None

        og_title = clean_text(_meta(prop="og:title") or "")
        desc = clean_text(
            _meta(prop="og:description")
            or _meta(name="description")
            or _meta(name="twitter:description")
            or ""
        )
        img = _meta(prop="og:image") or _meta(prop="og:image:secure_url") or _meta(name="twitter:image")
        if img:
            img = urljoin(page_url, img)

        # published time is optional; keep simple (many pages do not expose)
        published_ts: Optional[int] = None
        pub = _meta(prop="article:published_time") or _meta(prop="og:updated_time") or ""
        if pub:
            try:
                # Normalize Z
                pub_norm = pub.replace("Z", "+00:00") if pub.endswith("Z") else pub
                dt = datetime.fromisoformat(pub_norm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                published_ts = int(dt.astimezone(timezone.utc).timestamp())
            except Exception:
                published_ts = None

        return desc, img, published_ts, og_title
    except Exception as e:
        log.debug(f"Meta extraction falhou ({page_url}): {e}")
        return "", None, None, ""


# =========================
# TRANSLATION (best-effort)
# =========================
def translate_pt(text: str, tcache: dict[str, str]) -> str:
    txt = clean_text(text)
    if not txt or not TRANSLATE_PT:
        return txt

    key = f"pt::{txt}"
    if key in tcache:
        return tcache[key]

    try:
        payload = {"q": txt, "source": "en", "target": "pt", "format": "text"}
        if LIBRETRANSLATE_API_KEY:
            payload["api_key"] = LIBRETRANSLATE_API_KEY

        r = SESSION.post(LIBRETRANSLATE_URL, data=payload, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        out = clean_text(data.get("translatedText", "")) or txt
        tcache[key] = out
        return out
    except Exception:
        return txt


# =========================
# SOURCES
# =========================
def fetch_vlr_rss(limit: int = 60) -> list[NewsItem]:
    rss_url = "https://www.vlr.gg/rss"
    xml_text = safe_get(rss_url)

    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    out: list[NewsItem] = []
    for it in channel.findall("item")[:limit]:
        title = clean_text(it.findtext("title") or "VLR News")
        link = clean_text(it.findtext("link") or "")
        desc = strip_html(it.findtext("description") or "")
        pub = it.findtext("pubDate") or ""

        pub_ts: Optional[int] = None
        try:
            if pub:
                pub_dt = parsedate_to_datetime(pub).astimezone(timezone.utc)
                pub_ts = int(pub_dt.timestamp())
        except Exception:
            pub_ts = None

        if link:
            out.append(
                NewsItem(
                    uid=f"vlr::{link}",
                    title=title,
                    url=link,
                    source="VLR.gg",
                    description=desc,
                    published_ts=pub_ts,
                )
            )
    return out


def looks_like_thespike_article(href: str) -> bool:
    if not href:
        return False
    if "/valorant/news/" not in href:
        return False
    last = href.rstrip("/").split("/")[-1]
    return last.isdigit()


def fetch_thespike_listing(limit: int = 60) -> list[NewsItem]:
    base = "https://www.thespike.gg"
    listing_url = f"{base}/br/valorant/news" if THESPIKE_LOCALE == "br" else f"{base}/valorant/news"

    html_text = safe_get(listing_url)
    soup = BeautifulSoup(html_text, "lxml")

    seen: set[str] = set()
    out: list[NewsItem] = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not looks_like_thespike_article(href):
            continue

        url = urljoin(listing_url, href)
        if url in seen:
            continue
        seen.add(url)

        # Listing titles are often placeholder ("Loading..."). We'll use og:title later.
        title = clean_text(a.get_text(" ", strip=True))
        if title.lower() == "loading...":
            title = ""

        out.append(
            NewsItem(
                uid=f"spike::{url}",
                title=title,
                url=url,
                source="THESPIKE.GG",
                description="",
                published_ts=None,
            )
        )

        if len(out) >= limit:
            break

    return out


# =========================
# DISCORD WEBHOOK
# =========================
def build_embed(item: NewsItem, tcache: dict[str, str]) -> dict:
    meta_desc, meta_img, meta_pub_ts, meta_title = extract_meta(item.url)

    raw_title = clean_text(item.title)
    if raw_title.lower() in {"loading...", "", "thespike news"}:
        raw_title = meta_title or "Notícia"

    desc_raw = clean_text(item.description) or meta_desc or "Clique para abrir a notícia."

    # Translation policy:
    # - Always translate VLR (normally EN)
    # - THESPIKE: only translate when locale is 'en' OR when user disables TRANSLATE_ONLY_VLR
    do_translate = TRANSLATE_PT and (
        item.source == "VLR.gg" or (not TRANSLATE_ONLY_VLR and THESPIKE_LOCALE == "en")
    )

    title_final = translate_pt(raw_title, tcache) if do_translate else raw_title
    desc_final = translate_pt(desc_raw, tcache) if do_translate else desc_raw

    ts = item.published_ts or meta_pub_ts or _now_ts()

    embed: dict = {
        "title": f"📰 {title_final}"[:256],
        "url": item.url,
        "description": (f"> {desc_final[:900]}" if desc_final else "")[:4096],
        "color": EMBED_COLOR,
        "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "footer": {"text": FOOTER_TEXT},
        "author": {
            "name": f"Fonte: {item.source}",
            "url": "https://www.vlr.gg" if item.source == "VLR.gg" else "https://www.thespike.gg",
            "icon_url": SOURCE_ICONS.get(item.source, ""),
        },
        "fields": [
            {"name": "🗞️ Fonte", "value": item.source, "inline": True},
            {"name": "🕒 Publicado", "value": f"<t:{ts}:R>", "inline": True},
            {"name": "🔗 Link", "value": f"[Abrir notícia]({item.url})", "inline": True},
        ],
    }

    if USE_VORAX_THUMBNAIL and VORAX_THUMB_URL:
        embed["thumbnail"] = {"url": VORAX_THUMB_URL}

    if meta_img:
        embed["image"] = {"url": meta_img}

    return embed


def post_to_discord(webhook_url: str, item: NewsItem, tcache: dict[str, str]) -> None:
    payload = {
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [build_embed(item, tcache)],
    }
    r = SESSION.post(webhook_url, json=payload, timeout=HTTP_TIMEOUT_SEC)
    r.raise_for_status()


# =========================
# QUEUE (balanced)
# =========================
def build_post_queue(vlr_items: list[NewsItem], spike_items: list[NewsItem], cache: dict[str, int]) -> list[NewsItem]:
    vlr_new = [x for x in vlr_items if x.uid not in cache][:max(0, MAX_VLR_PER_RUN)]
    spike_new = [x for x in spike_items if x.uid not in cache][:max(0, MAX_THESPIKE_PER_RUN)]

    queue: list[NewsItem] = []
    i = 0
    while len(queue) < MAX_POSTS_PER_RUN and (vlr_new or spike_new):
        if i % 2 == 0:
            queue.append(vlr_new.pop(0) if vlr_new else spike_new.pop(0))
        else:
            queue.append(spike_new.pop(0) if spike_new else vlr_new.pop(0))
        i += 1
    return queue


# =========================
# MAIN
# =========================
def main() -> None:
    webhook_url = read_webhook_url()
    if not webhook_url or "discord.com/api/webhooks" not in webhook_url:
        raise SystemExit(
            "Webhook não definido.\n"
            "✅ Opção A (recomendado): crie 'webhook_url.txt' na pasta do bot com a URL (1 linha)\n"
            "✅ Opção B: setx DISCORD_WEBHOOK_URL \"<sua_url>\" e reabra o PowerShell"
        )

    cache = load_cache()
    tcache = load_translate_cache()

    vlr_items: list[NewsItem] = []
    spike_items: list[NewsItem] = []

    if ENABLE_VLR:
        try:
            vlr_items = fetch_vlr_rss(limit=60)
        except Exception as e:
            log.warning(f"Falha ao buscar VLR: {e}")

    if ENABLE_THESPIKE:
        try:
            spike_items = fetch_thespike_listing(limit=60)
        except Exception as e:
            log.warning(f"Falha ao buscar THESPIKE: {e}")

    log.info(f"Encontrados: VLR={len(vlr_items)} | THESPIKE={len(spike_items)}")

    queue = build_post_queue(vlr_items, spike_items, cache)
    if not queue:
        log.info("Nada novo para postar (ou já foi tudo postado).")
        return

    posted_now = 0
    for item in queue:
        try:
            post_to_discord(webhook_url, item, tcache)
            cache[item.uid] = _now_ts()
            posted_now += 1
            log.info(f"Postado: {item.source} — {item.title or item.url}")
            time.sleep(SLEEP_BETWEEN_POSTS_SEC)
        except Exception as e:
            log.warning(f"Falha ao postar '{item.title}': {e}")

    save_cache(cache)
    save_translate_cache(tcache)
    log.info(f"Postados agora: {posted_now}")


if __name__ == "__main__":
    main()
