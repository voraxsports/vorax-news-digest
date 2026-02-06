# -*- coding: utf-8 -*-
"""
Vorax News Digest — VLR.gg + THESPIKE.GG -> Discord Webhook
Rodando no GitHub Actions (sem PC ligado)

- 1 execução = 1 mensagem com até 4 embeds (2 VLR + 2 THESPIKE)
- Embeds padrão ORG (autor Vorax + thumbnail fixa + footer)
- Puxa imagem da notícia via og:image quando existir
- Anti-duplicado por posted_cache.json (persistido via commit do workflow)
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


# -------------------------
# CONFIG
# -------------------------
ROOT = Path(__file__).resolve().parent

# ✅ Webhook vem do GitHub Secret: DISCORD_WEBHOOK_URL
WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL", "") or os.getenv("WEBHOOK_URL", "")).strip()

# Brand / Visual
EMBED_COLOR = int(os.getenv("EMBED_COLOR", "0"))  # #000000
BRAND_NAME = os.getenv("BRAND_NAME", "Vorax eSports").strip()
FOOTER_TEXT = os.getenv("FOOTER_TEXT", "Vorax eSports • Notícias da Comunidade").strip()

# ✅ Logo fixa (sem query params pra não expirar)
VORAX_LOGO_URL = os.getenv(
    "VORAX_LOGO_URL",
    "https://cdn.discordapp.com/attachments/1440760869964484738/1469322383449002004/vorax_icon_alt_on-ghost.png.png",
).strip()

USE_VORAX_THUMBNAIL = os.getenv("USE_VORAX_THUMBNAIL", "1") != "0"

# Limites por execução (seu pedido: 2 + 2 por horário)
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "4"))
MAX_VLR_PER_RUN = int(os.getenv("MAX_VLR_PER_RUN", "2"))
MAX_THESPIKE_PER_RUN = int(os.getenv("MAX_THESPIKE_PER_RUN", "2"))

# HTTP
HTTP_TIMEOUT_SEC = int(os.getenv("HTTP_TIMEOUT_SEC", "25"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "2"))

# Cache anti-duplicado
CACHE_FILE = Path(os.getenv("CACHE_FILE", str(ROOT / "posted_cache.json")))
CACHE_KEEP_DAYS = int(os.getenv("CACHE_KEEP_DAYS", "30"))

# Fontes
ENABLE_VLR = os.getenv("ENABLE_VLR", "1") != "0"
ENABLE_THESPIKE = os.getenv("ENABLE_THESPIKE", "1") != "0"
THESPIKE_LOCALE = os.getenv("THESPIKE_LOCALE", "br").strip().lower()  # br|en

# Tradução best-effort (por padrão traduz só VLR)
TRANSLATE_PT = os.getenv("TRANSLATE_PT", "1") != "0"
TRANSLATE_ONLY_VLR = os.getenv("TRANSLATE_ONLY_VLR", "1") != "0"
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.de/translate").strip()
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
TRANSLATE_CACHE_FILE = Path(os.getenv("TRANSLATE_CACHE_FILE", str(ROOT / "translate_cache.json")))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(message)s")
log = logging.getLogger("vorax-news-digest")

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 VoraxNewsDigest/5.1",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
)


@dataclass(frozen=True)
class NewsItem:
    uid: str
    title: str
    url: str
    source: str
    description: str = ""
    published_ts: Optional[int] = None


# -------------------------
# UTILS
# -------------------------
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


def _request(method: str, url: str, **kwargs) -> requests.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            resp = SESSION.request(method, url, timeout=HTTP_TIMEOUT_SEC, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as ex:
            last_exc = ex
            if attempt < HTTP_RETRIES:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise last_exc


def safe_get(url: str) -> str:
    return _request("GET", url).text


# -------------------------
# CACHE
# -------------------------
def load_cache() -> dict[str, int]:
    if not CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): int(v) for k, v in raw.items()}
        if isinstance(raw, list):
            return {str(x): _now_ts() for x in raw}
    except Exception as ex:
        log.warning(f"Cache inválido, ignorando: {ex}")
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
    if len(cache) > 2000:
        for k in list(cache.keys())[: len(cache) - 2000]:
            cache.pop(k, None)
    TRANSLATE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# -------------------------
# META EXTRACTION (og:image, og:desc, published)
# -------------------------
def extract_meta(page_url: str) -> tuple[str, Optional[str], Optional[int], str]:
    """
    Returns: (description, og_image_url, published_ts, og_title)
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

        published_ts: Optional[int] = None
        pub = _meta(prop="article:published_time") or _meta(prop="og:updated_time") or ""
        if pub:
            try:
                pub_norm = pub.replace("Z", "+00:00") if pub.endswith("Z") else pub
                dt = datetime.fromisoformat(pub_norm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                published_ts = int(dt.astimezone(timezone.utc).timestamp())
            except Exception:
                published_ts = None

        return desc, img, published_ts, og_title
    except Exception:
        return "", None, None, ""


# -------------------------
# TRANSLATION (best-effort)
# -------------------------
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

        r = _request("POST", LIBRETRANSLATE_URL, data=payload)
        out = clean_text(r.json().get("translatedText", "")) or txt
        tcache[key] = out
        return out
    except Exception:
        return txt


# -------------------------
# SOURCES
# -------------------------
def fetch_vlr_rss(limit: int = 80) -> list[NewsItem]:
    xml_text = safe_get("https://www.vlr.gg/rss")
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


def _looks_like_thespike_article(href: str) -> bool:
    if not href or "/valorant/news/" not in href:
        return False
    last = href.rstrip("/").split("/")[-1]
    return last.isdigit()


def fetch_thespike_listing(limit: int = 80) -> list[NewsItem]:
    base = "https://www.thespike.gg"
    urls = (
        [f"{base}/br/valorant/news", f"{base}/valorant/news"]
        if THESPIKE_LOCALE == "br"
        else [f"{base}/valorant/news", f"{base}/br/valorant/news"]
    )

    for listing_url in urls:
        try:
            html_text = safe_get(listing_url)
            soup = BeautifulSoup(html_text, "lxml")

            out: list[NewsItem] = []
            seen: set[str] = set()

            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not _looks_like_thespike_article(href):
                    continue

                url = urljoin(listing_url, href)
                if url in seen:
                    continue
                seen.add(url)

                title = clean_text(a.get_text(" ", strip=True))
                if title.lower() == "loading...":
                    title = ""

                out.append(NewsItem(uid=f"spike::{url}", title=title, url=url, source="THESPIKE.GG"))
                if len(out) >= limit:
                    break

            if len(out) >= 5:
                return out
            if out:
                return out
        except Exception:
            continue

    return []


# -------------------------
# DISCORD EMBEDS (ORG STYLE)
# -------------------------
def _author_block() -> dict:
    return {
        "name": f"{BRAND_NAME} • News Digest",
        "url": "https://x.com/Voraxsports",
        "icon_url": VORAX_LOGO_URL or "",
    }


def build_embed(item: NewsItem, tcache: dict[str, str]) -> dict:
    meta_desc, meta_img, meta_pub_ts, meta_title = extract_meta(item.url)

    raw_title = clean_text(item.title)
    if raw_title.lower() in {"loading...", "", "thespike news"}:
        raw_title = meta_title or "Notícia"

    desc_raw = clean_text(item.description) or meta_desc or "Clique para abrir a notícia."

    # Tradução: por padrão só VLR
    do_translate = TRANSLATE_PT and (item.source == "VLR.gg" or (not TRANSLATE_ONLY_VLR and THESPIKE_LOCALE == "en"))
    title_final = translate_pt(raw_title, tcache) if do_translate else raw_title
    desc_final = translate_pt(desc_raw, tcache) if do_translate else desc_raw

    ts = item.published_ts or meta_pub_ts or _now_ts()
    source_home = "https://www.vlr.gg/news" if item.source == "VLR.gg" else "https://www.thespike.gg/br/valorant/news"
    source_label = "VLR.gg" if item.source == "VLR.gg" else "THESPIKE.GG"

    embed: dict = {
        "author": _author_block(),
        "title": title_final[:256],
        "url": item.url,
        "description": (f"**Resumo:**\n> {desc_final[:900]}")[:4096],
        "color": EMBED_COLOR,
        "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "footer": {"text": FOOTER_TEXT},
        "fields": [
            {"name": "🗞️ Fonte", "value": f"[{source_label}]({source_home})", "inline": True},
            {"name": "🕒 Publicado", "value": f"<t:{ts}:R>", "inline": True},
            {"name": "🔗 Acessar", "value": f"[Abrir notícia]({item.url})", "inline": True},
            {"name": "📍 Canal", "value": "#noticias-da-comunidade", "inline": True},
            {"name": "📌 Padrão Vorax", "value": "Curadoria • Disciplina • Competitividade", "inline": True},
        ],
    }

    # Logo fixa no canto do embed
    if USE_VORAX_THUMBNAIL and VORAX_LOGO_URL:
        embed["thumbnail"] = {"url": VORAX_LOGO_URL}

    # Imagem da notícia
    if meta_img:
        embed["image"] = {"url": meta_img}

    return embed


def post_digest(items: list[NewsItem], tcache: dict[str, str]) -> None:
    embeds = [build_embed(x, tcache) for x in items][:10]
    now = _now_ts()
    payload = {
        "content": f"🗞️ **Digest de Notícias** • VLR.gg + TheSpike.gg • <t:{now}:t> • <t:{now}:R>",
        "allowed_mentions": {"parse": []},
        "embeds": embeds,
    }
    _request("POST", WEBHOOK_URL, json=payload)


# -------------------------
# QUEUE
# -------------------------
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


def main() -> None:
    if not WEBHOOK_URL or "discord.com/api/webhooks" not in WEBHOOK_URL:
        raise SystemExit("Webhook não definido. No GitHub: crie o Secret 'DISCORD_WEBHOOK_URL'.")

    cache = load_cache()
    tcache = load_translate_cache()

    vlr_items: list[NewsItem] = []
    spike_items: list[NewsItem] = []

    if ENABLE_VLR:
        try:
            vlr_items = fetch_vlr_rss()
        except Exception as ex:
            log.warning(f"Falha ao buscar VLR: {ex}")

    if ENABLE_THESPIKE:
        try:
            spike_items = fetch_thespike_listing()
        except Exception as ex:
            log.warning(f"Falha ao buscar THESPIKE: {ex}")

    log.info(f"Encontrados: VLR={len(vlr_items)} | THESPIKE={len(spike_items)}")

    queue = build_post_queue(vlr_items, spike_items, cache)
    if not queue:
        log.info("Nada novo para postar (ou já foi tudo postado).")
        return

    post_digest(queue, tcache)

    now = _now_ts()
    for item in queue:
        cache[item.uid] = now

    save_cache(cache)
    save_translate_cache(tcache)

    log.info(f"Postados agora: {len(queue)}")


if __name__ == "__main__":
    main()
