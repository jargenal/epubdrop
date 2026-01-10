import json
import os
import re
import requests
import shutil
import zipfile
import xml.etree.ElementTree as ET

from django.conf import settings
from ebooklib import epub, ITEM_DOCUMENT, ITEM_IMAGE, ITEM_STYLE, ITEM_FONT
from lxml import html as lxml_html
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# ============================================================
# HTML sanitization (trusted EPUBs) + optional CSS sanitization
# ============================================================

try:
    # Optional: allows safe "style" attr sanitization (requires tinycss2)
    from bleach.css_sanitizer import CSSSanitizer  # type: ignore

    _CSS_SANITIZER = CSSSanitizer(
        allowed_css_properties=[
            # layout/text
            "color", "background-color", "font-weight", "font-style", "text-decoration",
            "text-align", "line-height", "font-size", "font-family",
            "margin", "margin-left", "margin-right", "margin-top", "margin-bottom",
            "padding", "padding-left", "padding-right", "padding-top", "padding-bottom",
            "border", "border-left", "border-right", "border-top", "border-bottom",
            "border-color", "border-width", "border-style",
            "width", "max-width", "min-width", "height", "max-height", "min-height",
            "display", "float", "clear",
            # tables
            "border-collapse", "border-spacing", "vertical-align",
        ],
        allowed_svg_properties=[],
    )
except Exception:
    _CSS_SANITIZER = None


import bleach  # bleach after optional CSS sanitizer


# Allow rich content: headings, tables, images, inline formatting, etc.
ALLOWED_TAGS = [
    # structure
    "div", "span", "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer",
    # lists
    "ul", "ol", "li",
    # emphasis
    "b", "strong", "i", "em", "u", "s", "sub", "sup",
    # links/media
    "a", "img", "figure", "figcaption",
    # quotes/code
    "blockquote", "pre", "code",
    # tables
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
]

ALLOWED_ATTRS = {
    "*": ["class", "id", "title", "aria-label", "role"],
    "a": ["href", "name", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height", "loading"],
    "table": ["summary"],
    "th": ["colspan", "rowspan", "scope"],
    "td": ["colspan", "rowspan"],
}

# If CSS sanitizer available, allow style; else avoid style to prevent unsafe CSS
if _CSS_SANITIZER is not None:
    for k in list(ALLOWED_ATTRS.keys()):
        if "style" not in ALLOWED_ATTRS[k]:
            ALLOWED_ATTRS[k].append("style")

ALLOWED_PROTOCOLS = ["http", "https", "mailto", "data"]  # data allows embedded images


def sanitize_html_trusted(html: str) -> str:
    """
    EPUBs de confianza (uso personal). Aun así limpiamos:
    - Remueve scripts/event handlers
    - Mantiene tags/attrs permitidos
    - Opcionalmente sanitiza estilos si tinycss2 está instalado
    """
    if not html:
        return ""

    kwargs: Dict[str, Any] = dict(
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    if _CSS_SANITIZER is not None:
        kwargs["css_sanitizer"] = _CSS_SANITIZER

    cleaned = bleach.clean(html, **kwargs)

    # Asegura rel seguro si target=_blank
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank],
        skip_tags=["pre", "code"],
    )
    return cleaned


# ============================================================
# EPUB upload validation
# ============================================================

def validate_epub_file(uploaded_file) -> Tuple[bool, Optional[str]]:
    """
    Retorna SIEMPRE: (is_valid, error_message)
    - extensión .epub
    - mimetype (cuando viene)
    - cabecera ZIP (PK)
    - archivo interno 'mimetype' en ZIP y que sea application/epub+zip
    """
    try:
        name = (getattr(uploaded_file, "name", "") or "").lower()
        if not name.endswith(".epub"):
            return False, "El archivo no tiene extensión .epub"

        content_type = (getattr(uploaded_file, "content_type", "") or "").lower().strip()
        # browsers sometimes send octet-stream
        allowed_mime = {"application/epub+zip", "application/octet-stream"}
        if content_type and content_type not in allowed_mime:
            return False, f"MIME no permitido: {content_type}. Se esperaba application/epub+zip"

        # Validate ZIP header without loading full file
        pos = None
        try:
            pos = uploaded_file.tell()
        except Exception:
            pos = None

        header = uploaded_file.read(4)
        try:
            if pos is not None:
                uploaded_file.seek(pos)
            else:
                uploaded_file.seek(0)
        except Exception:
            pass

        if header[:2] != b"PK":
            return False, "El archivo no parece ser un ZIP válido (cabecera incorrecta)."

        # Check internal mimetype file
        try:
            zf = zipfile.ZipFile(uploaded_file)
            names = zf.namelist()
            if "mimetype" in names:
                mimedata = zf.read("mimetype").decode("utf-8", errors="replace").strip()
                if mimedata != "application/epub+zip":
                    return False, "El EPUB tiene un mimetype interno inválido."
            else:
                # Puedes flexibilizar esto si tienes EPUBs viejos
                return False, "El EPUB no contiene el archivo interno 'mimetype' en la raíz."
        except zipfile.BadZipFile:
            return False, "El archivo no es un ZIP válido (BadZipFile)."
        finally:
            try:
                uploaded_file.seek(0)
            except Exception:
                pass

        return True, None
    except Exception as e:
        return False, f"No se pudo validar el archivo: {e}"


# ============================================================
# Book state persistence (JSON) by book_id
# ============================================================

def _book_dir(book_id: str) -> Path:
    return Path(settings.MEDIA_ROOT) / "epub_books" / book_id


def save_book_state(book_id: str, payload: Dict[str, Any]) -> None:
    d = _book_dir(book_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "book.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_book_state(book_id: str) -> Optional[Dict[str, Any]]:
    p = _book_dir(book_id) / "book.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_original_block_html(sections: List[Dict[str, Any]], section_idx: int, block_idx: int) -> Optional[str]:
    try:
        return sections[section_idx]["blocks"][block_idx]
    except Exception:
        return None


# ============================================================
# Safe extraction of EPUB assets to MEDIA (images/css/fonts)
# ============================================================

def _safe_extract_zip(zip_path: str, dest_dir: Path) -> None:
    """
    Prevent Zip Slip (path traversal).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = Path(member.filename)

            # Ignore absolute paths and traversal
            if member_path.is_absolute():
                continue
            if ".." in member_path.parts:
                continue

            target_path = dest_dir / member.filename
            target_path.parent.mkdir(parents=True, exist_ok=True)

            if member.is_dir():
                continue

            with zf.open(member, "r") as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def extract_epub_assets_to_media(epub_path: str, book_id: str) -> Path:
    """
    Extracts the entire EPUB zip into:
      MEDIA_ROOT/epub_assets/<book_id>/
    Returns the absolute path to that directory.
    """
    assets_dir = Path(settings.MEDIA_ROOT) / "epub_assets" / book_id
    # re-extract fresh
    if assets_dir.exists():
        shutil.rmtree(assets_dir, ignore_errors=True)

    _safe_extract_zip(epub_path, assets_dir)
    return assets_dir


# ============================================================
# Helpers: media type, cover detection, text stats
# ============================================================

def _item_media_type(item) -> str:
    # ebooklib items differ by version; be defensive
    for attr in ("get_media_type", "media_type", "content_type", "get_type"):
        try:
            v = getattr(item, attr)
            if callable(v):
                out = v()
            else:
                out = v
            if isinstance(out, str) and out:
                return out
        except Exception:
            continue
    return ""


def _guess_ext_from_media_type(mt: str) -> str:
    mt = (mt or "").lower()
    if "jpeg" in mt or "jpg" in mt:
        return ".jpg"
    if "png" in mt:
        return ".png"
    if "gif" in mt:
        return ".gif"
    if "webp" in mt:
        return ".webp"
    if "svg" in mt:
        return ".svg"
    return ""


def _strip_html_to_text(html: str) -> str:
    # fast: remove tags; good enough for counts
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _estimate_pages_from_words(word_count: int) -> int:
    # rough estimate: 250 words per page
    if word_count <= 0:
        return 0
    return max(1, int(round(word_count / 250.0)))


def _find_cover_id_from_opf_meta(book: epub.EpubBook) -> Optional[str]:
    """
    OPF meta sometimes includes: <meta name="cover" content="cover-image-id" />
    ebooklib exposes metadata, but not always. We'll parse from book.get_metadata.
    """
    try:
        metas = book.get_metadata("OPF", "meta")
        for meta in metas:
            # meta is tuple like (attrs_dict, value)
            attrs = meta[0] if meta else {}
            if isinstance(attrs, dict):
                if attrs.get("name") == "cover" and attrs.get("content"):
                    return attrs["content"]
    except Exception:
        pass
    return None


def _pick_cover_item(book: epub.EpubBook):
    """
    Try multiple strategies to locate cover item.
    """
    # 1) ebooklib cover type
    try:
        for it in book.get_items_of_type(ITEM_IMAGE):
            # some ebooks mark cover in id/name
            name = (it.get_name() or "").lower()
            if "cover" in name:
                return it
    except Exception:
        pass

    # 2) meta cover id
    cover_id = _find_cover_id_from_opf_meta(book)
    if cover_id:
        try:
            it = book.get_item_with_id(cover_id)
            if it:
                return it
        except Exception:
            pass

    # 3) book.get_cover() sometimes works
    try:
        # returns (file_name, content)
        cov = book.get_cover()
        if cov and isinstance(cov, tuple) and len(cov) == 2:
            name, content = cov
            return ("__raw__", name, content)
    except Exception:
        pass

    return None


def _save_cover_to_assets(book: epub.EpubBook, assets_dir: Path) -> Optional[str]:
    """
    Saves cover to assets_dir/_cover.<ext>
    Returns relative path inside assets_dir (posix style) e.g. "_cover.jpg"
    """
    cover = _pick_cover_item(book)
    if not cover:
        return None

    try:
        if isinstance(cover, tuple) and cover[0] == "__raw__":
            _, name, content = cover
            mt = ""
            ext = Path(name).suffix or ""
            if not ext:
                ext = ".jpg"
            out_name = f"_cover{ext}"
            (assets_dir / out_name).write_bytes(content)
            return out_name.replace("\\", "/")

        item = cover
        content = item.get_content()
        name = item.get_name() or "cover"
        mt = _item_media_type(item)
        ext = Path(name).suffix or _guess_ext_from_media_type(mt) or ".jpg"
        out_name = f"_cover{ext}"
        (assets_dir / out_name).write_bytes(content)
        return out_name.replace("\\", "/")
    except Exception:
        return None


# ============================================================
# URL rewrite: resolve relative src/href + optional style url(...)
# ============================================================

_ATTR_URL_RE = re.compile(r'''(?P<attr>\s(?:src|href)=["'])(?P<url>[^"']+)(["'])''', re.IGNORECASE)
_STYLE_URL_RE = re.compile(r"""url\((?P<q>['"]?)(?P<u>[^'")]+)(?P=q)\)""", re.IGNORECASE)


def _is_absolute_url(u: str) -> bool:
    u = (u or "").strip()
    if not u:
        return True
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:", "data:", "#", "javascript:")):
        return True
    return bool(urlparse(u).scheme)


def rewrite_relative_urls(html: str, doc_base_url: str) -> str:
    """
    Resolve relative src/href and style url(...) against the XHTML directory base.
    doc_base_url must end with "/".
    """
    if not html or not doc_base_url:
        return html or ""

    if not doc_base_url.endswith("/"):
        doc_base_url += "/"

    def repl(m):
        attr = m.group("attr")
        url = (m.group("url") or "").strip()
        tail = m.group(3)

        if _is_absolute_url(url):
            return m.group(0)

        fixed = urljoin(doc_base_url, url)
        return f"{attr}{fixed}{tail}"

    out = _ATTR_URL_RE.sub(repl, html)

    # also rewrite url(...) inside inline style attributes if present
    def repl_style(m):
        u = (m.group("u") or "").strip()
        if _is_absolute_url(u):
            return m.group(0)
        fixed = urljoin(doc_base_url, u)
        return f"url('{fixed}')"

    out = _STYLE_URL_RE.sub(repl_style, out)
    return out


# ============================================================
# Split HTML into sections + blocks (best-effort)
# ============================================================

def split_html_by_blocks(html: str, max_section_chars: int = 12000) -> List[str]:
    """
    Split long HTML into smaller chunks by block boundaries (best-effort).
    If bs4 is available, use it; otherwise fallback to no split.
    """
    if not html:
        return [""]

    if len(html) <= max_section_chars:
        return [html]

    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        # fallback: no reliable splitting
        return [html]

    soup = BeautifulSoup(html, "html.parser")
    body = soup.body or soup

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for child in body.children:
        if getattr(child, "name", None) is None:
            # text node
            s = str(child).strip()
            if not s:
                continue
            piece = s
        else:
            piece = str(child)

        if current_len + len(piece) > max_section_chars and current:
            chunks.append("".join(current))
            current = [piece]
            current_len = len(piece)
        else:
            current.append(piece)
            current_len += len(piece)

    if current:
        chunks.append("".join(current))

    return chunks if chunks else [html]


def split_section_html_into_blocks(section_html: str) -> List[str]:
    """
    Divide una sección HTML en 'bloques' alineables:
    - p, h1-h6, blockquote, pre, table, ul/ol, figure, img, hr, etc.
    - Si hay <div> contenedores con muchos bloques dentro, se 'aplanan' para mantener párrafo-a-párrafo.
    Resultado: lista de HTML strings, cada uno representando 1 bloque visual.
    """
    if not section_html or not section_html.strip():
        return ["<p></p>"]

    # Wrap para parsear fragmentos sin romper
    wrapped = f"<div id='__wrap__'>{section_html}</div>"

    try:
        root = lxml_html.fromstring(wrapped)
    except Exception:
        return [section_html]

    wrap = root.get_element_by_id("__wrap__")

    BLOCK_TAGS = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "pre", "table", "ul", "ol",
        "figure", "img", "hr"
    }

    def serialize(el) -> str:
        # tostring conserva tags y atributos
        return lxml_html.tostring(el, encoding="unicode", with_tail=False)

    def is_block(el) -> bool:
        return (getattr(el, "tag", "") or "").lower() in BLOCK_TAGS

    def flatten(node) -> List[Any]:
        """
        Retorna una lista de elementos (o strings) que representan bloques.
        Aplana contenedores para no mezclar múltiples párrafos en un bloque.
        """
        out: List[Any] = []

        # Texto directo del contenedor -> párrafo
        text = (node.text or "").strip()
        if text:
            out.append(f"<p>{lxml_html.escape(text)}</p>")

        for child in list(node):
            tag = (child.tag or "").lower()

            # Si el hijo ya es un bloque, lo agregamos tal cual
            if is_block(child):
                out.append(child)
            else:
                # Si es un contenedor (div/section/article/span), intentamos aplanar su contenido.
                # Esto es clave para "párrafo por párrafo".
                # Si dentro hay bloques, los extraemos.
                inner_blocks = flatten(child)
                if inner_blocks:
                    out.extend(inner_blocks)
                else:
                    # Si es inline o vacío, lo tratamos como un párrafo conservando su HTML
                    html_str = serialize(child).strip()
                    if html_str:
                        out.append(f"<p>{html_str}</p>")

            # Tail del hijo -> también puede tener texto
            tail = (child.tail or "").strip()
            if tail:
                out.append(f"<p>{lxml_html.escape(tail)}</p>")

        return out

    items = flatten(wrap)

    blocks: List[str] = []
    for it in items:
        if isinstance(it, str):
            html_str = it.strip()
        else:
            html_str = serialize(it).strip()

        if html_str:
            blocks.append(html_str)

    if not blocks:
        blocks = ["<p></p>"]

    return blocks


def _get_opf_dir_from_epub(epub_path: str) -> str:
    """
    Lee META-INF/container.xml y devuelve el directorio donde vive el OPF.
    Ej: 'OEBPS' si full-path='OEBPS/content.opf'
    """
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            container_xml = zf.read("META-INF/container.xml")
        root = ET.fromstring(container_xml)

        # container.xml suele usar namespaces
        # Buscamos rootfile[@full-path]
        full_path = None
        for elem in root.iter():
            if elem.tag.lower().endswith("rootfile"):
                full_path = elem.attrib.get("full-path") or elem.attrib.get("fullpath")
                if full_path:
                    break

        if not full_path:
            return ""

        full_path = full_path.replace("\\", "/").lstrip("/")
        opf_dir = os.path.dirname(full_path).replace("\\", "/").strip("/")
        return opf_dir
    except Exception:
        return ""

# ============================================================
# Reader: build sections from spine (doc_base_url per XHTML dir)
# ============================================================

def build_reader_sections_with_blocks_from_spine(
    local_epub_path: str,
    assets_root_url: str,
    max_section_chars: int = 12000,
) -> List[Dict[str, Any]]:
    """
    assets_root_url = /media/epub_assets/<book_id>/
    Corrige rutas usando OPF dir (ej: OEBPS/), para que src/href apunten al path real extraído.
    """
    book = epub.read_epub(local_epub_path)
    out: List[Dict[str, Any]] = []
    sec_idx = 0

    if assets_root_url and not assets_root_url.endswith("/"):
        assets_root_url += "/"

    opf_dir = _get_opf_dir_from_epub(local_epub_path)  # <-- CLAVE
    if opf_dir:
        opf_dir = opf_dir.strip("/")

    for item_id, _linear in (book.spine or []):
        item = book.get_item_with_id(item_id)
        if not item:
            continue

        name = (item.get_name() or "").replace("\\", "/").lstrip("/")
        low = name.lower()
        if not low.endswith((".xhtml", ".html", ".htm")):
            continue

        raw = item.get_content()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")

        # Si ebooklib nos da 'Text/ch1.xhtml' pero el OPF vive en 'OEBPS/',
        # los recursos reales están bajo OEBPS/. Prependemos OEBPS/ al name
        # SOLO si todavía no viene incluido.
        effective_name = name
        if opf_dir:
            prefix = opf_dir + "/"
            if not effective_name.startswith(prefix):
                effective_name = prefix + effective_name

        doc_dir = os.path.dirname(effective_name).replace("\\", "/").strip("/")
        if doc_dir:
            doc_base_url = f"{assets_root_url}{doc_dir}/"
        else:
            doc_base_url = assets_root_url

        raw = rewrite_relative_urls(raw, doc_base_url)
        cleaned = sanitize_html_trusted(raw)

        chunks = split_html_by_blocks(cleaned, max_section_chars=max_section_chars)
        for ch in chunks:
            blocks = split_section_html_into_blocks(ch)
            out.append({"id": "s%d" % sec_idx, "blocks": blocks})
            sec_idx += 1

    return out


# ============================================================
# LibreTranslate integration (HTML format)
# ============================================================

def translate_html_with_libretranslate(html: str) -> str:
    """
    Translate HTML using a local LibreTranslate endpoint.
    Endpoint default: http://127.0.0.1:5050/translate
    """
    endpoint = os.getenv("LIBRETRANSLATE_URL", "http://127.0.0.1:5050/translate")

    r = requests.post(
        endpoint,
        data={
            "q": html,
            "source": "en",
            "target": "es",
            "format": "html",
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("translatedText", "") or ""


# ============================================================
# extract_epub_info_from_path (NOW accepts book_id safely)
# ============================================================

def extract_epub_info_from_path(epub_path: str, book_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Extracts relevant metadata and stats.
    If book_id is provided:
      - extracts all assets to MEDIA_ROOT/epub_assets/<book_id>/
      - saves cover to assets as _cover.<ext>
      - returns cover_url and assets_base_url
    """
    info: Dict[str, Any] = {}

    book = epub.read_epub(epub_path)

    # ------------------------------------------------------------
    # If book_id provided, extract full EPUB zip for asset serving
    # ------------------------------------------------------------
    assets_dir: Optional[Path] = None
    assets_base_url: Optional[str] = None
    if book_id:
        assets_dir = extract_epub_assets_to_media(epub_path, book_id)
        assets_base_url = f"{settings.MEDIA_URL}epub_assets/{book_id}/"
        info["assets_base_url"] = assets_base_url

        # cover save (best effort)
        rel_cover = _save_cover_to_assets(book, assets_dir)
        if rel_cover:
            info["cover_url"] = f"{assets_base_url}{rel_cover}"

    # ------------------------------------------------------------
    # Metadata (best effort; ebooklib returns list of tuples)
    # ------------------------------------------------------------
    def _first_meta(ns: str, name: str) -> Optional[str]:
        try:
            m = book.get_metadata(ns, name)
            if m:
                # (value, attrs)
                v = m[0][0]
                return v.strip() if isinstance(v, str) else str(v)
        except Exception:
            pass
        return None

    title = _first_meta("DC", "title") or "Libro"
    language = _first_meta("DC", "language")
    publisher = _first_meta("DC", "publisher")
    rights = _first_meta("DC", "rights")
    date = _first_meta("DC", "date")
    identifier = _first_meta("DC", "identifier")

    authors: List[str] = []
    try:
        creators = book.get_metadata("DC", "creator") or []
        for c in creators:
            v = c[0]
            if isinstance(v, str) and v.strip():
                authors.append(v.strip())
    except Exception:
        pass

    subjects: List[str] = []
    try:
        subs = book.get_metadata("DC", "subject") or []
        for s in subs:
            v = s[0]
            if isinstance(v, str) and v.strip():
                subjects.append(v.strip())
    except Exception:
        pass

    description_html = _first_meta("DC", "description") or ""
    # Keep description HTML but sanitize
    description_html = sanitize_html_trusted(description_html) if description_html else ""

    info.update({
        "title": title,
        "language": language,
        "publisher": publisher,
        "rights": rights,
        "date": date,
        "identifier": identifier,
        "authors": authors,
        "subjects": subjects,
        "description_html": description_html,
        "file_size_bytes": int(Path(epub_path).stat().st_size) if Path(epub_path).exists() else None,
    })

    # ------------------------------------------------------------
    # Resource stats
    # ------------------------------------------------------------
    doc_count = 0
    img_count = 0
    css_count = 0
    font_count = 0

    try:
        for it in book.get_items():
            t = it.get_type()
            if t == ITEM_DOCUMENT:
                doc_count += 1
            elif t == ITEM_IMAGE:
                img_count += 1
            elif t == ITEM_STYLE:
                css_count += 1
            elif t == ITEM_FONT:
                font_count += 1
    except Exception:
        pass

    spine_len = 0
    try:
        spine_len = len(book.spine or [])
    except Exception:
        spine_len = 0

    toc_len = 0
    try:
        toc_len = len(book.toc or [])
    except Exception:
        toc_len = 0

    info["resources"] = {
        "spine_items": spine_len,
        "toc_entries": toc_len,
        "documents": doc_count,
        "images": img_count,
        "stylesheets": css_count,
        "fonts": font_count,
    }

    # ------------------------------------------------------------
    # Text stats (from spine documents)
    # ------------------------------------------------------------
    total_text = ""
    try:
        for item_id, _linear in (book.spine or []):
            it = book.get_item_with_id(item_id)
            if not it:
                continue
            name = (it.get_name() or "").lower()
            if not name.endswith((".xhtml", ".html", ".htm")):
                continue
            raw = it.get_content()
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            total_text += " " + _strip_html_to_text(raw)
    except Exception:
        pass

    total_text = re.sub(r"\s+", " ", total_text).strip()
    word_count = len(total_text.split()) if total_text else 0
    char_count = len(total_text) if total_text else 0

    info["text_stats"] = {
        "word_count": word_count,
        "char_count": char_count,
        "estimated_pages": _estimate_pages_from_words(word_count),
    }

    return info