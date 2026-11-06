"""
Muse 貼文截錄器 V3.1 - Notion + Obsidian 雙存
Fixes over the shipped V3 main.py:
- Added POST /api/collect-web (was referenced by frontend/automation but missing)
- /api/collect now accepts destination on social posts; /api/collect-web the same
- GET / serves frontend.html, GET /api/health for platform health checks
- CORS enabled so the frontend can call the API
- Notion write auto-detects the database schema:
  * V3 English schema (URL/Platform/Author/Caption/PostDate/...)
  * Existing Kai 收藏庫 Chinese schema (名稱/作者/來源連結/平台/封面/建立時間/標籤/狀態)
  Full caption/content is also appended as page blocks so nothing is lost
  when the database has no Caption/FullContent property.
- Apify path actually calls an actor (was a `pass` placeholder), yt-dlp fallback kept
- Obsidian REST push now includes the configured folder path, and is verified
- Optional shared-secret: set COLLECTOR_API_KEY to require header X-API-Key
"""
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
_TW = timezone(timedelta(hours=8))
import os, re, pathlib, requests
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Muse Collector V3.1 - Notion + Obsidian")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DESTINATIONS = ("notion", "obsidian", "both")


class CollectPayload(BaseModel):
    url: str
    destination: str = "both"  # notion | obsidian | both


def check_key(x_api_key: str | None):
    expected = os.getenv("COLLECTOR_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def detect_platform(url: str) -> str:
    l = url.lower()
    if "instagram.com" in l or "instagr.am" in l:
        return "IG"
    if "threads.com" in l or "threads.net" in l:
        return "Threads"
    if "x.com" in l or "twitter.com" in l:
        return "X"
    return "Web"


# ---------- extraction ----------
def extract_with_apify_or_ytdlp(url: str):
    platform = detect_platform(url)
    token = os.getenv("APIFY_TOKEN")
    if token:
        try:
            from apify_client import ApifyClient
            client = ApifyClient(token)
            if platform == "Threads":
                # This actor takes post URLs and returns one record whose
                # nested "thread" object carries username/text/images/videos.
                actor_id = "logical_scrapers/threads-post-scraper"
                run_input = {"startUrls": [{"url": url}], "includeReplies": False, "maxReplies": 0}
            elif platform == "X":
                actor_id = "apidojo/twitter-scraper"
                run_input = {"directUrls": [url], "resultsLimit": 20}
            else:
                actor_id = "apify/instagram-scraper"
                run_input = {"directUrls": [url], "resultsLimit": 20}
            run = client.actor(actor_id).call(run_input=run_input)
            ds_id = None
            if isinstance(run, dict):
                ds_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
            elif run is not None:
                ds_id = getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None)
            items = client.dataset(ds_id).list_items().items if ds_id else []
            if items:
                it = items[0]
                t = it.get("thread") if isinstance(it.get("thread"), dict) else it
                media = []
                def add_media(v, kind="image"):
                    if isinstance(v, str):
                        media.append({"url": v, "kind": kind})
                    elif isinstance(v, dict):
                        u = v.get("url") or v.get("displayUrl") or v.get("videoUrl")
                        if u:
                            media.append({"url": u, "kind": kind})
                    elif isinstance(v, list):
                        for x in v:
                            add_media(x, kind)
                for key in ("images", "displayUrl", "childPosts", "media"):
                    add_media(t.get(key), "image")
                for key in ("videos", "videoUrl"):
                    add_media(t.get(key), "video")
                return {
                    "author": t.get("username") or t.get("ownerUsername") or t.get("author") or "unknown",
                    "caption": t.get("text") or t.get("caption") or "",
                    "media": media,
                    "timestamp": None,
                    "source": "apify",
                }
        except Exception as e:
            print(f"Apify failed, fallback yt-dlp: {e}")
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "author": info.get("uploader") or info.get("channel") or "unknown",
                "caption": info.get("description") or info.get("title") or "",
                "media": info.get("thumbnails", []),
                "timestamp": info.get("timestamp"),
                "source": "yt-dlp",
            }
    except Exception as e:
        return {"author": "unknown", "caption": "", "media": [], "timestamp": None,
                "source": "none", "error": str(e)}



# ---------- Threads page parser (gets carousels, linked/quoted posts, self-thread) ----------
_GOOGLEBOT_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"


def _threads_code(url: str):
    import re as _re
    m = _re.search(r"/post/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _post_media(p: dict):
    out = []
    items = p.get("carousel_media") or [p]
    for it in items:
        if not isinstance(it, dict):
            continue
        vids = it.get("video_versions") or []
        if vids and vids[0].get("url"):
            out.append({"url": vids[0]["url"], "kind": "video"})
            continue
        cands = (it.get("image_versions2") or {}).get("candidates") or []
        if cands and cands[0].get("url"):
            best = max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
            out.append({"url": best["url"], "kind": "image"})
    return out


def extract_threads_html(url: str):
    """Read the public Threads post page and pull caption + every image/video,
    including a linked/quoted post (e.g. a post that embeds another post's
    10-image carousel) and the author's own follow-up posts in the thread."""
    import json as _json, re as _re
    code = _threads_code(url)
    r = requests.get(url, timeout=20, headers={"User-Agent": _GOOGLEBOT_UA, "Accept-Language": "zh-TW,zh;q=0.9"})
    r.raise_for_status()
    final_code = _threads_code(r.url) or code
    blobs = _re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', r.text, _re.S)
    posts = {}

    def walk(o):
        if isinstance(o, dict):
            c = o.get("code")
            if c and isinstance(o.get("user"), dict) and ("carousel_media" in o or "image_versions2" in o):
                if c not in posts or len(_json.dumps(o)) > len(_json.dumps(posts[c])):
                    posts[c] = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for b in blobs:
        if final_code and final_code not in b:
            continue
        try:
            walk(_json.loads(b))
        except Exception:
            pass
    main = posts.get(final_code)
    if not main:
        return None
    author = main["user"].get("username") or "unknown"
    texts = [((main.get("caption") or {}).get("text") or "")]
    media = _post_media(main)
    info = main.get("text_post_app_info") or {}
    share = info.get("share_info") or {}
    for linked in (info.get("linked_inline_media"), share.get("quoted_post"),
                   share.get("quoted_attachment_post"), share.get("reposted_post")):
        if isinstance(linked, dict):
            lt = (linked.get("caption") or {}).get("text") or ""
            lu = (linked.get("user") or {}).get("username") or ""
            if lt:
                texts.append(f"—— 引用 @{lu} ——\n{lt}")
            media += _post_media(linked)
    # author's own follow-ups in the same thread
    for c, p in posts.items():
        if c == final_code or (p.get("user") or {}).get("username") != author:
            continue
        pi = p.get("text_post_app_info") or {}
        if pi.get("is_reply") and ((pi.get("reply_to_author") or {}).get("username") == author):
            t = (p.get("caption") or {}).get("text") or ""
            if t:
                texts.append(t)
            media += _post_media(p)
    seen, uniq = set(), []
    for m in media:
        if m["url"] not in seen:
            seen.add(m["url"])
            uniq.append(m)
    return {"author": author, "caption": "\n\n".join(t for t in texts if t),
            "media": uniq, "timestamp": main.get("taken_at"), "source": "threads-html"}


def extract_web(url: str):
    try:
        import trafilatura
        from bs4 import BeautifulSoup
        downloaded = trafilatura.fetch_url(url)
        main_text = trafilatura.extract(downloaded) if downloaded else ""
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
        og = soup.find("meta", property="og:image")
        og_image = og.get("content", "") if og else ""
        dtag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
        desc = dtag.get("content", "") if dtag else ""
        images = [img.get("src") for img in soup.find_all("img") if img.get("src")][:10]
        return {"title": title, "description": desc, "og_image": og_image,
                "content": main_text or "", "images": images,
                "domain": re.sub(r"https?://", "", url).split("/")[0],
                "author": title, "caption": desc}
    except Exception as e:
        return {"title": "", "description": "", "og_image": "", "content": "",
                "images": [], "domain": "", "error": str(e)}


# ---------- Obsidian ----------
def generate_obsidian_markdown(data: dict, platform: str, url: str):
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_author = re.sub(r"[^a-zA-Z0-9_-]", "", str(data.get("author", "unknown")))[:20] or "unknown"
    filename = f"{date_str}-{platform}-{safe_author}.md"
    fm = (f"---\nsource: {url}\nplatform: {platform}\n"
          f"author: {data.get('author', '')}\ndate: {date_str}\n"
          f"tags: [clippings, {platform.lower()}]\ndomain: {data.get('domain', '')}\n---\n\n")
    body = f"# {data.get('title') or data.get('author') or platform}\n\n{(data.get('caption') or data.get('content', ''))[:8000]}\n\n"
    media = [m.get("url") if isinstance(m, dict) else m for m in data.get("media", [])]
    for u in [x for x in media if x][:10] + list(data.get("images", []))[:10]:
        body += f"![image]({u})\n\n"
    if data.get("og_image"):
        body += f"![image]({data['og_image']})\n\n"
    return filename, fm + body


def save_obsidian(data, platform, url):
    fname, md = generate_obsidian_markdown(data, platform, url)
    folder = os.getenv("OBSIDIAN_FOLDER", "Clippings/Social").strip("/")
    api_url = os.getenv("OBSIDIAN_API_URL")
    api_token = os.getenv("OBSIDIAN_API_TOKEN")
    if api_url and api_token:
        target = f"{folder}/{fname}" if folder else fname
        resp = requests.put(f"{api_url.rstrip('/')}/vault/{target}",
                            data=md.encode("utf-8"),
                            headers={"Authorization": f"Bearer {api_token}"}, timeout=20)
        resp.raise_for_status()
        return f"pushed via REST API as {target}"
    out_path = pathlib.Path("/tmp") / fname
    out_path.write_text(md, encoding="utf-8")
    return {"note": f"generated {fname} at {out_path} (no OBSIDIAN_API_URL set; file is ephemeral on the host)",
            "filename": fname, "obsidian_markdown": md[:8000]}


def _notion_upload_image(img_url: str, headers: dict):
    """Copy an image into Notion (File Upload API) so it never expires.
    Returns the file_upload id, or None to fall back to an external link."""
    import requests as _rq
    try:
        g = _rq.get(img_url, timeout=30, headers={"User-Agent": _GOOGLEBOT_UA})
        g.raise_for_status()
        ctype = g.headers.get("Content-Type", "image/jpeg").split(";")[0]
        ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
        name = f"image.{ext}"
        h = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        c = _rq.post("https://api.notion.com/v1/file_uploads", headers=headers,
                     json={"filename": name, "content_type": ctype}, timeout=30)
        c.raise_for_status()
        fid = c.json()["id"]
        s = _rq.post(f"https://api.notion.com/v1/file_uploads/{fid}/send", headers=h,
                     files={"file": (name, g.content, ctype)}, timeout=60)
        s.raise_for_status()
        return fid
    except Exception as e:
        print(f"Notion upload failed, using external url: {e}")
        return None


def _make_title(t: str, limit: int = 40) -> str:
    import re as _re
    for line in (t or "").splitlines():
        line = _re.sub(r"\s+", " ", line).strip(" -—⬇️👇#")
        if line and not line.startswith("#"):
            return line if len(line) <= limit else line[:limit].rstrip() + "…"
    return (t or "")[:limit] or "未命名"


# ---------- Notion ----------
def save_notion(data, platform, url):
    # Raw HTTP against the 2025-09-03 API: databases created under the new
    # data-source model return no properties from databases.retrieve, so the
    # schema is read from the database's first data source and the page is
    # created with a data_source_id parent.
    import requests as _rq
    token = os.getenv("NOTION_TOKEN")
    db_id = os.getenv("NOTION_DATABASE_ID")
    if not token:
        raise RuntimeError("NOTION_TOKEN not set")
    if not db_id:
        raise RuntimeError("NOTION_DATABASE_ID not set")
    headers = {"Authorization": f"Bearer {token}",
               "Notion-Version": "2025-09-03",
               "Content-Type": "application/json"}
    r = _rq.get(f"https://api.notion.com/v1/databases/{db_id}", headers=headers, timeout=30)
    r.raise_for_status()
    db = r.json()
    schema = db.get("properties", {}) or {}
    parent = {"database_id": db_id}
    if db.get("data_sources"):
        ds_id = db["data_sources"][0]["id"]
        r2 = _rq.get(f"https://api.notion.com/v1/data_sources/{ds_id}", headers=headers, timeout=30)
        r2.raise_for_status()
        ds_schema = r2.json().get("properties", {}) or {}
        if ds_schema:
            schema = ds_schema
            parent = {"data_source_id": ds_id}
    names = set(schema.keys())
    text = (data.get("caption") or data.get("content") or data.get("description") or "")
    title_text = _make_title(data.get("title") or text or url)
    cover_url = data.get("og_image") or next(
        (m.get("url") for m in data.get("media", [])
         if isinstance(m, dict) and m.get("url") and m.get("kind", "image") != "video"), None)
    ts = data.get("timestamp")
    try:
        post_date = datetime.fromtimestamp(int(ts), _TW).date().isoformat() if ts else datetime.now(_TW).date().isoformat()
    except Exception:
        post_date = datetime.now(_TW).date().isoformat()
    # upload every image once; reuse the first as the cover
    uploaded = {}
    def _fid(u):
        if u not in uploaded:
            uploaded[u] = _notion_upload_image(u, headers)
        return uploaded[u]
    cover_fid = _notion_upload_image(cover_url, headers) if cover_url else None  # separate upload for the property
    props = {}
    if "URL" in names:  # V3 English schema
        props["URL"] = {"title": [{"text": {"content": url}}]}
        if "Platform" in names:
            props["Platform"] = {"select": {"name": platform}}
        if "Author" in names:
            props["Author"] = {"rich_text": [{"text": {"content": str(data.get("author", ""))[:1900]}}]}
        if "Caption" in names:
            props["Caption"] = {"rich_text": [{"text": {"content": text[:1900]}}]}
        if "Domain" in names and data.get("domain"):
            props["Domain"] = {"rich_text": [{"text": {"content": data["domain"][:1900]}}]}
        if "FullContent" in names and data.get("content"):
            props["FullContent"] = {"rich_text": [{"text": {"content": data["content"][:1900]}}]}
        if "PostDate" in names:
            props["PostDate"] = {"date": {"start": post_date}}
    elif "名稱" in names:  # Existing Kai 收藏庫 schema
        props["名稱"] = {"title": [{"text": {"content": title_text}}]}
        if "作者" in names:
            props["作者"] = {"rich_text": [{"text": {"content": str(data.get("author", ""))[:1900]}}]}
        if "來源連結" in names:
            props["來源連結"] = {"url": url}
        if "平台" in names:
            plat_map = {"IG": "instagram", "Threads": "threads"}
            if platform in plat_map:  # X / Web options don't exist in that DB; skip instead of failing
                props["平台"] = {"select": {"name": plat_map[platform]}}
        if "封面" in names and cover_url:
            if cover_fid:
                props["封面"] = {"files": [{"name": "cover", "type": "file_upload", "file_upload": {"id": cover_fid}}]}
            else:
                props["封面"] = {"files": [{"name": "cover", "external": {"url": cover_url}}]}
        if "建立時間" in names:
            props["建立時間"] = {"date": {"start": post_date}}
        if "狀態" in names:
            props["狀態"] = {"select": {"name": "未整理"}}
        if "標籤" in names:
            props["標籤"] = {"multi_select": [{"name": "收藏"}]}
    else:
        raise RuntimeError(f"Unrecognised Notion schema: {sorted(names)}")
    # ----- page body: 貼文內容 / 引用 / 圖片 / 來源 -----
    def _rt(t, link=None):
        d = {"type": "text", "text": {"content": t[:1900]}}
        if link:
            d["text"]["link"] = {"url": link}
        return [d]
    def _h(t):
        return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(t)}}
    def _paras(t, kind="paragraph"):
        out = []
        for chunk in [c.strip() for c in t.split("\n\n") if c.strip()]:
            for i in range(0, len(chunk), 1900):
                out.append({"object": "block", "type": kind, kind: {"rich_text": _rt(chunk[i:i+1900])}})
        return out
    children = []
    main_text, quoted = text, []
    if "—— 引用 @" in text:
        parts = text.split("—— 引用 @")
        main_text = parts[0].strip()
        for q in parts[1:]:
            head, _, rest = q.partition("——")
            quoted.append((head.strip(), rest.strip()))
    if main_text:
        children.append(_h("貼文內容"))
        children += _paras(main_text)
    for who, qt in quoted:
        children.append(_h(f"引用 @{who}"))
        children += _paras(qt, "quote")
    imgs, vids, seen_media = [], [], set()
    for m in data.get("media", []):
        mu, mkind = (m.get("url"), m.get("kind", "image")) if isinstance(m, dict) else (m, "image")
        if not mu or mu in seen_media:
            continue
        seen_media.add(mu)
        (vids if mkind == "video" else imgs).append(mu)
    if imgs:
        children.append(_h(f"圖片（{len(imgs)}）"))
        for mu in imgs[:20]:
            fid = _fid(mu)
            if fid:
                children.append({"object": "block", "type": "image",
                                 "image": {"type": "file_upload", "file_upload": {"id": fid}}})
            else:
                children.append({"object": "block", "type": "image",
                                 "image": {"type": "external", "external": {"url": mu}}})
    if vids:
        children.append(_h("影片"))
        for i, mu in enumerate(vids[:10], 1):
            children.append({"object": "block", "type": "paragraph",
                             "paragraph": {"rich_text": _rt(f"影片 {i}", mu)}})
    children.append({"object": "block", "type": "divider", "divider": {}})
    children.append({"object": "block", "type": "paragraph",
                     "paragraph": {"rich_text": [{"type": "text", "text": {"content": "來源："}}] + _rt(url, url)}})
    resp = _rq.post("https://api.notion.com/v1/pages", headers=headers,
                    json={"parent": parent, "properties": props,
                          "children": children[:100]}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Notion pages.create {resp.status_code}: {resp.text[:400]}")
    page = resp.json()
    return {"ok": True, "page_id": page.get("id"), "page_url": page.get("url")}


# ---------- shared collect flow ----------
def run_collect(url: str, destination: str, force_platform: str | None = None):
    if destination not in DESTINATIONS:
        raise HTTPException(status_code=400, detail=f"destination must be one of {DESTINATIONS}")
    platform = force_platform or detect_platform(url)
    data = extract_web(url) if platform == "Web" else extract_with_apify_or_ytdlp(url)
    if platform == "Threads":
        try:
            h = extract_threads_html(url)
            if h and (len(h["media"]) > len(data.get("media") or []) or len(h["caption"]) > len(data.get("caption") or "")):
                if not h["media"]:
                    h["media"] = data.get("media") or []
                data = h
        except Exception as e:
            print(f"Threads HTML parse failed: {e}")
    data["platform"] = platform
    data["url"] = url
    results = {}
    if destination in ("notion", "both"):
        try:
            results["notion"] = save_notion(data, platform, url)
        except Exception as e:
            results["notion"] = f"error: {e}"
    if destination in ("obsidian", "both"):
        try:
            results["obsidian"] = save_obsidian(data, platform, url)
        except Exception as e:
            results["obsidian"] = f"error: {e}"
    preview = {k: str(v)[:500] for k, v in data.items()}
    return {"ok": True, "platform": platform, "destination": destination,
            "results": results, "data": preview}


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Muse Collector V3.1",
            "notion_configured": bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_DATABASE_ID")),
            "apify_configured": bool(os.getenv("APIFY_TOKEN")),
            "obsidian_api_configured": bool(os.getenv("OBSIDIAN_API_URL") and os.getenv("OBSIDIAN_API_TOKEN"))}

@app.get("/api/key-check")
def key_check(x_api_key: str | None = Header(default=None)):
    import hashlib
    expected = os.getenv("COLLECTOR_API_KEY") or ""
    received = x_api_key or ""
    def fp(s):
        return hashlib.sha256(s.encode()).hexdigest()[:8] if s else None
    return {"received_present": bool(received), "received_len": len(received),
            "expected_len": len(expected), "match": bool(expected) and received == expected,
            "received_fp": fp(received), "expected_fp": fp(expected)}


@app.post("/api/collect")
def collect(payload: CollectPayload, x_api_key: str | None = Header(default=None)):
    check_key(x_api_key)
    return run_collect(payload.url, payload.destination)


@app.post("/api/collect-web")
def collect_web(payload: CollectPayload, x_api_key: str | None = Header(default=None)):
    check_key(x_api_key)
    return run_collect(payload.url, payload.destination, force_platform="Web")


@app.get("/app")
def frontend():
    p = pathlib.Path(__file__).parent / "frontend.html"
    if p.exists():
        return FileResponse(p)
    return JSONResponse({"detail": "frontend.html not found"}, status_code=404)


@app.get("/")
def root():
    return {"status": "Muse Collector V3.1 - Notion + Obsidian ready",
            "endpoints": ["/api/health", "/api/collect", "/api/collect-web", "/app"]}
