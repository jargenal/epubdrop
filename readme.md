# Epubdrop (Django) — EPUB Upload + Biblioteca + Lector Bilingüe (LibreTranslate)

Proyecto personal en Django 4.2.27 para **cargar EPUBs**, **extraer contenido enriquecido**, y **leerlos en un lector bilingüe** con **traducción local gratuita** usando **LibreTranslate en Docker**.  
El lector muestra **Original (derecha)** y **Traducción (izquierda)**, alineado **por bloque/párrafo** (grid 2 columnas) y con **traducción lazy** para mantener la experiencia fluida.

---

## 1) Funcionalidades implementadas / previstas

### ✅ Implementado (actual)
- **Upload de EPUB** (drag & drop) + validación básica:
  - extensión `.epub`
  - validación de MIME type (según configuración del proyecto)
- **Guardado en disco** (evita cargar en memoria archivos grandes)
- **Extracción de información relevante**:
  - metadata (título, autor, descripción)
  - portada (cuando es posible obtenerla)
  - estructura en secciones/bloques (para lectura)
- **Render de contenido enriquecido**:
  - respeta HTML básico, negritas, listas, tablas e imágenes (si assets están bien servidos)
- **Lector bilingüe (read.html)**:
  - 2 columnas: **traducción** (izq) / **original** (der)
  - alineación **por bloque/párrafo** usando **CSS Grid** (sin medir alturas por JS)
  - **traducción lazy** (IntersectionObserver + lookahead + concurrencia limitada)
  - UI profesional con Tailwind, **modo claro**, lectura limpia
  - configuración: tamaño y tipo de letra
- **Botón Limpiar** (borrado del libro en disco y estado asociado)

### 🔜 Previsto / Próximos pasos (escala)
- **Biblioteca**: página con listado de libros cargados (portada, progreso, estado)
- **Persistencia de traducción**:
  - en DB o filesystem por bloque para no depender del cache en memoria
- **Batch translation** (traducción por lotes) para reducir latencia
- **Progreso de lectura persistente** (retomar donde lo dejaste)
- **Búsqueda** en el libro (original y traducción)
- **Bookmarks / notas**
- **Navegación por TOC** (tabla de contenidos)

---

## 2) Stack técnico

- **Python**: 3.9.x (relevante: evita `X | None`, usar `Optional[X]`)
- **Django**: 4.2.27
- **EPUB parsing**: ebooklib (y utilidades para HTML)
- **HTML parsing / normalización**: (dependiendo de tu implementación, ej. lxml)
- **Frontend**: Tailwind (CDN en templates)
- **Traducción local**: LibreTranslate en Docker (endpoint local)
- **Assets**: `MEDIA_ROOT` + `MEDIA_URL` para servir imágenes/CSS extraídos

---

## 3) Estructura de proyecto (referencial)