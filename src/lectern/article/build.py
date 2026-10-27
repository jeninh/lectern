"""The readable article in a web page, and a clean EPUB 3 built from it.

:func:`extract` finds the article the way reader modes do, with lxml and no network: the page's
``<article>`` (or ``itemprop="articleBody"``) when there is one with real text, else the
element whose paragraphs score highest (text length and commas, less link-heavy text) plus
its siblings that score well too. Navigation, sharing, comment, cookie, and advertising blocks
are removed by tag and by class/id first. Title, byline, site, date, language, description,
and the lead image come from the page's metadata (Open Graph, ``article:*``, ``<title>``).

:func:`build_epub` writes it with the MOBI conversion's machinery (lectern.ingest.kindle): the
body is rebuilt from the same allowlist of elements and attributes (no scripts, forms, frames,
event handlers, styles that load anything, or remote resources), images are only ones that
were downloaded and re-encoded here (:func:`normalise_image`), links to the web stay links,
and the package is deterministic (fixed timestamps and order): saving the same article twice
makes the same file, which import catches as a duplicate.
"""

from __future__ import annotations

import copy
import io
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from lectern.ingest import kindle

MIN_TEXT = 140  # characters of article text below which a page has no article
IMAGE_MAX_SIDE = 1600
MAX_IMAGE_PIXELS = 40_000_000
STYLE = """\
body { line-height: 1.5 }
header.article-head { margin-bottom: 1.5em }
header.article-head p { margin: 0.2em 0; font-size: 0.9em }
img { max-width: 100%; height: auto }
figure { margin: 1em 0 }
figcaption { font-size: 0.85em }
blockquote { margin: 0 0 0 1.25em }
pre { white-space: pre-wrap }
"""

_DROP_TAGS = (
    "script style noscript template iframe frame frameset object embed form button input "
    "select textarea nav aside footer dialog svg canvas video audio source track link meta"
).split()
_UNLIKELY = re.compile(
    r"-ad-|\bads?\b|advert|agegate|banner|breadcrumb|combx|comment|community|cookie|consent|"
    r"disqus|donat|footer|gdpr|masthead|menu|modal|newsletter|outbrain|pager|pagination|popup|"
    r"promo|related|remark|replies|rss|share|sharing|shoutbox|sidebar|signup|skyscraper|"
    r"social|sponsor|subscribe|taboola|toolbar|widget",
    re.I,
)
_MAYBE = re.compile(r"and|article|body|column|content|entry|main|post|shadow|story|text", re.I)
_POSITIVE = re.compile(r"article|body|content|entry|main|page|post|story|text|blog", re.I)
_NEGATIVE = re.compile(
    r"-ad-|hidden|banner|combx|comment|com-|contact|footer|masthead|media|meta|outbrain|promo|"
    r"related|scroll|share|shoutbox|sidebar|skyscraper|sponsor|shopping|tags|tool|widget",
    re.I,
)
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


class ArticleError(ValueError):
    """``code``: ``no_article`` (nothing readable on the page)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Article:
    url: str
    title: str
    byline: str | None
    site: str
    published: str | None  # YYYY-MM-DD
    language: str | None
    description: str | None
    content: object  # an lxml element: the article's blocks, links absolute
    lead_image: str | None  # absolute http(s) URL (Open Graph image)
    image_urls: list[str] = field(default_factory=list)  # in reading order, unique


def _text(el) -> str:
    return " ".join(el.text_content().split())


def _clean(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    value = "".join(c for c in value if c in "\t\n" or ord(c) >= 0x20)
    value = " ".join(value.split())[:limit]
    return value or None


def _http_url(base: str, value: str | None) -> str | None:
    if not value or value.strip().lower().startswith("data:"):
        return None
    url = urljoin(base, value.strip())
    return url if urlsplit(url).scheme in ("http", "https") and len(url) <= 2048 else None


def _parse(data: bytes, charset: str | None):
    from lxml import etree, html

    for encoding in (charset, None):
        try:
            parser = html.HTMLParser(
                encoding=encoding, no_network=True, remove_comments=True, remove_pis=True
            )
            return html.document_fromstring(data, parser=parser)
        except LookupError:
            continue  # an encoding name Python does not know: let lxml detect it
        except (etree.ParserError, ValueError):
            break
    raise ArticleError("no_article")


def _meta(root) -> dict[str, str]:
    out: dict[str, str] = {}
    for el in root.iter("meta"):
        key = (el.get("property") or el.get("name") or el.get("itemprop") or "").strip().lower()
        content = el.get("content")
        if key and content and key not in out:
            out[key] = content
    return out


def _metadata(root, url: str) -> dict:
    meta = _meta(root)
    title = meta.get("og:title") or meta.get("twitter:title")
    if not title:
        h1 = next(root.iter("h1"), None)
        title = _text(h1) if h1 is not None else None
    if not title:
        t = root.find(".//title")
        title = _text(t) if t is not None else None
    byline = meta.get("author") or meta.get("article:author") or meta.get("parsely-author")
    if byline and urlsplit(byline.strip()).scheme in ("http", "https"):
        byline = None  # article:author is often a profile URL
    if not byline:
        rel = next((a for a in root.iter("a") if (a.get("rel") or "").lower() == "author"), None)
        byline = _text(rel) if rel is not None else None
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    site = _clean(meta.get("og:site_name"), 128) or host or "Web"
    published = None
    for key in ("article:published_time", "datepublished", "date", "dc.date", "pubdate"):
        m = _DATE.match((meta.get(key) or "").strip())
        if m:
            published = m.group(1)
            break
    if published is None:
        t = next((t for t in root.iter("time") if _DATE.match(t.get("datetime") or "")), None)
        if t is not None:
            published = _DATE.match(t.get("datetime")).group(1)
    lang = (root.get("lang") or root.get("xml:lang") or "").strip()
    if not lang and meta.get("og:locale"):
        lang = meta["og:locale"].replace("_", "-")
    lang = lang if kindle._LANG.match(lang) else None
    return {
        "title": _clean(title, 512) or host or "Untitled article",
        "byline": _clean(byline, 256),
        "site": site,
        "published": published,
        "language": lang,
        "description": _clean(meta.get("og:description") or meta.get("description"), 2000),
        "lead_image": _http_url(url, meta.get("og:image") or meta.get("twitter:image")),
    }


def _attrs_of(el) -> str:
    return f"{el.get('class') or ''} {el.get('id') or ''}"


def _remove(el) -> None:
    parent = el.getparent()
    if parent is not None:
        el.drop_tree()  # keeps the tail text in place


def _strip_junk(root) -> None:
    for el in list(root.iter(*_DROP_TAGS)):
        _remove(el)
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or el.tag in ("html", "body", "article", "main", "a"):
            continue
        attrs = _attrs_of(el)
        hidden = el.get("hidden") is not None or el.get("aria-hidden") == "true"
        hidden = hidden or "display:none" in (el.get("style") or "").replace(" ", "")
        if hidden or (_UNLIKELY.search(attrs) and not _MAYBE.search(attrs)):
            if el.getparent() is not None:
                _remove(el)


def _class_weight(el) -> int:
    attrs = _attrs_of(el)
    return (25 if _POSITIVE.search(attrs) else 0) - (25 if _NEGATIVE.search(attrs) else 0)


def _link_density(el) -> float:
    total = len(_text(el))
    if not total:
        return 0.0
    return sum(len(_text(a)) for a in el.iter("a")) / total


_BASE = {"div": 5, "article": 10, "section": 3, "pre": 3, "td": 3, "blockquote": 3}


def _best(root):
    """The element holding the article: a real ``<article>`` first, else the best scorer."""
    for path in (".//*[@itemprop='articleBody']", ".//article", ".//main"):
        found = [el for el in root.findall(path) if len(_text(el)) >= 500]
        if len(found) == 1:
            return found[0]
    scores: dict = {}
    for p in root.iter("p", "pre", "td", "blockquote"):
        text = _text(p)
        if len(text) < 25:
            continue
        score = 1 + text.count(",") + min(len(text) // 100, 3)
        parent = p.getparent()
        grand = parent.getparent() if parent is not None else None
        for node, share in ((parent, 1.0), (grand, 0.5)):
            if node is None or not isinstance(node.tag, str):
                continue
            if node not in scores:
                scores[node] = _BASE.get(node.tag, 0) + _class_weight(node)
            scores[node] += score * share
    if not scores:
        return None
    ranked = {el: s * (1 - _link_density(el)) for el, s in scores.items()}
    best = max(ranked, key=ranked.get)
    parent = best.getparent()
    if parent is None:
        return best
    # Paragraphs a page split over sibling blocks belong together.
    limit = max(10.0, ranked[best] * 0.2)
    keep = []
    for sib in parent:
        if not isinstance(sib.tag, str):
            continue
        if sib is best or ranked.get(sib, -1) >= limit:
            keep.append(sib)
        elif sib.tag == "p":
            text = _text(sib)
            if len(text) > 80 and _link_density(sib) < 0.25:
                keep.append(sib)
    if len(keep) == 1:
        return best
    from lxml import html

    wrapper = html.Element("div")
    for sib in keep:
        clone = copy.deepcopy(sib)
        clone.tail = None
        wrapper.append(clone)
    return wrapper


def _image_source(img, base: str) -> str | None:
    """The real image behind lazy-loading placeholders (``data-src``, ``srcset``)."""
    for key in ("data-src", "data-original", "data-lazy-src", "data-url", "src"):
        url = _http_url(base, img.get(key))
        if url:
            return url
    for key in ("srcset", "data-srcset"):
        candidates = [c.strip().split(" ")[0] for c in (img.get(key) or "").split(",")]
        for c in reversed([c for c in candidates if c]):  # usually ascending widths
            url = _http_url(base, c)
            if url:
                return url
    return None


def _tidy(content, title: str, base: str) -> list[str]:
    """Absolute links, chosen image sources, link-heavy leftovers removed, the duplicate
    title dropped. Returns the image URLs in reading order."""
    for el in list(content.iter("header")):
        if el.getparent() is not None and len(_text(el)) < 400:
            _remove(el)  # the article's own title block: ours replaces it
    for a in content.iter("a"):
        href = a.get("href")
        if href and not href.startswith("#"):
            url = urljoin(base, href.strip())
            if urlsplit(url).scheme in ("http", "https", "mailto"):
                a.set("href", url)
            else:
                del a.attrib["href"]
    images: list[str] = []
    for img in list(content.iter("img")):
        url = _image_source(img, base)
        if url is None:
            _remove(img)
            continue
        for key in list(img.attrib):
            if key not in ("alt", "title", "width", "height"):
                del img.attrib[key]
        img.set("src", url)
        if url not in images:
            images.append(url)
    for el in list(content.iter("div", "section", "ul", "ol", "table")):
        if el.getparent() is None:
            continue
        text = _text(el)
        if not el.findall(".//img") and len(text) < 200 and _link_density(el) > 0.5:
            _remove(el)
    for h1 in list(content.iter("h1", "h2")):
        if h1.getparent() is not None and _text(h1).casefold() == title.casefold():
            _remove(h1)
    for p in list(content.iter("p")):
        if p.getparent() is not None and not _text(p) and not p.findall(".//img"):
            _remove(p)
    return images


def extract(data: bytes, url: str, charset: str | None = None) -> Article:
    """The readable article in an HTML page fetched from ``url``. Raises :class:`ArticleError`
    ``no_article`` when there is none (a page made by scripts, a login wall, an error page)."""
    root = _parse(data, charset)
    info = _metadata(root, url)
    base = url
    base_el = root.find(".//base")
    if base_el is not None and _http_url(url, base_el.get("href")):
        base = _http_url(url, base_el.get("href"))
    _strip_junk(root)
    content = _best(root)
    if content is None or len(_text(content)) < MIN_TEXT:
        raise ArticleError("no_article")
    content = copy.deepcopy(content)
    content.tail = None
    images = _tidy(content, info["title"], base)
    if len(_text(content)) < MIN_TEXT:
        raise ArticleError("no_article")
    return Article(url=url, content=content, image_urls=images, **info)


# --- images ----------------------------------------------------------------------------------


def normalise_image(data: bytes) -> tuple[bytes, str] | None:
    """A downloaded image, decoded and re-encoded (JPEG, or PNG when it has transparency) at
    most :data:`IMAGE_MAX_SIDE` pixels a side: what e-readers show well, with nothing of the
    original file left but its pixels. ``None`` for anything else (SVG, a tracking pixel, a
    decompression bomb, a broken file)."""
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ("JPEG", "PNG", "GIF", "WEBP"):
                return None
            w, h = im.size
            if w * h > MAX_IMAGE_PIXELS or w < 16 or h < 16:
                return None
            im.seek(0)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            alpha = im.mode in ("RGBA", "LA", "PA") or (
                im.mode == "P" and "transparency" in im.info
            )
            out = io.BytesIO()
            if alpha:
                im.convert("RGBA").save(out, "PNG", optimize=True)
                return out.getvalue(), "image/png"
            im.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - PIL raises many kinds; any of them means "not an image"
        return None


# --- the EPUB ----------------------------------------------------------------------------------


def _head(article: Article):
    from lxml import html

    head = html.Element("header")
    head.set("class", "article-head")
    h1 = html.Element("h1")
    h1.text = article.title
    head.append(h1)
    parts = [p for p in (article.byline, article.site, article.published) if p]
    if parts:
        p = html.Element("p")
        p.text = " · ".join(parts)
        head.append(p)
    p = html.Element("p")
    p.text = "Saved from "
    a = html.Element("a")
    a.set("href", article.url)
    a.text = urlsplit(article.url).hostname or article.url
    p.append(a)
    head.append(p)
    return head


def build_epub(article: Article, images: dict[str, tuple[bytes, str]], out: Path) -> dict:
    """Write the article to ``out`` as an EPUB 3. ``images``: downloaded and normalised images
    by URL (:func:`normalise_image`); images that are not in it are left out. The lead image,
    when downloaded, is the cover. Returns what went in."""
    from lxml import html

    book = kindle.Book()
    names: dict[str, str] = {}
    ext = {"image/jpeg": "jpg", "image/png": "png"}
    for url, (data, media) in images.items():
        name = f"images/{len(names) + 1:03d}.{ext[media]}"
        names[url] = name
        book.resources[name] = (data, media)
    content = copy.deepcopy(article.content)
    for img in list(content.iter("img")):
        name = names.get(img.get("src") or "")
        if name is None:
            _remove(img)
        else:
            img.set("src", name)
            if img.get("alt") is None:
                img.set("alt", "")
    root = html.Element("html")
    body = html.Element("body")
    root.append(body)
    body.append(_head(article))
    if content.tag in ("article", "main", "div", "section", "body"):
        content.tag = "div"
    body.append(content)
    xbody = kindle.Sanitizer(mobi=False).body(root)
    doc = kindle.Doc(
        name="article.xhtml",
        body=xbody,
        title=article.title,
        lang=article.language,
        stylesheets=["style.css"],
    )
    kindle._dedupe_ids(doc)
    book.docs[doc.name] = doc
    book.spine.append((doc.name, True))
    book.toc.append((article.title, doc.name))
    book.resources["style.css"] = (STYLE.encode(), "text/css")
    if article.lead_image in names:
        book.cover = names[article.lead_image]
    meta = {
        "uid": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'lectern:article:' + article.url)}",
        "title": article.title,
        "authors": [article.byline or article.site],
        "language": article.language or "und",
        "isbn": None,
        "asin": None,
        "publisher": article.site,
        "description": article.description,
        "date": article.published,
        "subjects": ["Article"],
        "source": article.url,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    kindle.write_epub(book, meta, out)
    used = sum(1 for _ in xbody.iter(f"{{{kindle.XHTML_NS}}}img"))
    return {"images": used, "cover": book.cover is not None}
