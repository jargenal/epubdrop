# EPUBDrop

EPUBDrop es una aplicación Django para cargar EPUBs, traducirlos con Ollama y leerlos en formato bilingüe con persistencia de progreso.

## Características del proyecto

- Biblioteca personal con autenticación por usuario.
- Biblioteca rediseñada con dos secciones principales:
  - `Tu Biblioteca` (sección con énfasis visual para exploración/lectura)
  - `Carga y Métricas` (sección compacta con carga, métricas y trabajos en traducción)
- Carga de EPUB con validación (extensión, MIME, ZIP/mimetype interno).
- Extracción de assets (imágenes, estilos, fuentes) a `media/epub_assets/<book_id>/`.
- Detección robusta de portada EPUB:
  - soporta metadata OPF en distintos formatos (`meta name=cover`)
  - considera ítems tipo cover y no solo tipo image
  - fallback por heurísticas en `id/name` (`cover`, `front`, `titlepage`)
- Persistencia por bloques:
  - `Book`, `Section`, `Block`
  - traducción HTML por bloque
  - progreso de lectura por usuario (`ReadingProgress`)
  - bookmarks por bloque
- Lector bilingüe en dos columnas:
  - izquierda traducido, derecha original
  - preservación de estructura HTML (títulos, listas, tablas, imágenes, enlaces)
  - scroll, TOC, búsqueda y bookmarks
- Tema visual claro/oscuro y selector persistente.
- Guardado de progreso robusto:
  - guardado remoto en backend
  - respaldo local por libro si falla red
  - indicador visual de estado de guardado
- Traducción async por libro (hilo de background).
- Caché de traducción (`TranslationCache`) para evitar retraducciones innecesarias.

## Calidad de traducción y prevención de fallos

El pipeline de traducción incluye controles para evitar respuestas “descriptivas” del modelo (por ejemplo: “No hay contenido HTML para traducir…”):

- Validación estructural de salida traducida:
  - rechaza salida vacía
  - rechaza texto meta/descriptivo/refusal
  - exige conservación de etiquetas estructurales
  - valida preservación de `img[src]` y `a[href]`
- Reintentos inteligentes con prompt reforzado cuando una salida es inválida.
- Si todos los reintentos fallan, usa fallback seguro al HTML original del bloque.
- Validación del caché:
  - si una entrada cacheada resulta inválida, se descarta y se regenera.
- Sanitización/auditoría post-traducción para reparar bloques inválidos sin borrar trabajo correcto.

## Stack técnico

- Python 3.9+
- Django 4.2.x
- SQLite (default)
- ebooklib + lxml + bleach
- Frontend con templates Django + Tailwind CDN + JS/CSS estáticos
- Ollama local (`/api/generate`)

## Biblioteca (UI)

- Vista `Tu Biblioteca` enfocada en catálogo:
  - tarjetas alineadas en `Mosaico` y `Galería`
  - contenido por tarjeta: portada, título y acciones
  - se removieron vistas `Detalle` y `Columna`
- Vista `Carga y Métricas` compacta:
  - formulario de carga
  - métricas operativas
  - lista `En traducción`

## Estructura relevante

- `config/`: settings y rutas globales.
- `reader/models.py`: entidades principales.
- `reader/views.py`: biblioteca, lectura, APIs de progreso/bookmarks/métricas.
- `reader/tasks.py`: traducción async por libro y sanitización post-proceso.
- `reader/utils.py`: parsing EPUB, traducción con Ollama, validación/sanitización.
- `reader/management/commands/audit_translations.py`: auditoría manual de traducciones.

## Configuración rápida

1. Crear entorno e instalar dependencias

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Variables de entorno (`.env`)

```env
OLLAMA_URL=http://127.0.0.1:11434/api/generate
OLLAMA_MODEL=translategemma:4b
OLLAMA_TIMEOUT_SECONDS=120
OLLAMA_MAX_RETRIES=3
OLLAMA_RETRY_BASE_SECONDS=0.6
```

3. Migraciones y arranque

```bash
python manage.py migrate
python manage.py runserver
```

## Ollama

```bash
brew install ollama
ollama serve
ollama pull translategemma:4b
```

## Auditoría de traducciones

La auditoría repara bloques inválidos sin eliminar traducciones válidas.

### 1) Auditar un libro específico

```bash
.venv/bin/python manage.py audit_translations --book-id <BOOK_UUID>
```

Salida esperada (ejemplo):

```text
[<BOOK_UUID>] scanned=5074 valid=5039 repaired=35 fallback_original=0
TOTAL books=1 scanned=5074 valid=5039 repaired=35 fallback_original=0
```

### 2) Auditar todos los libros listos

```bash
.venv/bin/python manage.py audit_translations --all-ready
```

### 3) Flujo recomendado de auditoría

1. Ejecuta auditoría por libro si detectas inconsistencias puntuales.
2. Ejecuta auditoría global (`--all-ready`) después de cambios de prompt/modelo.
3. Revisa campos `repaired` y `fallback_original` en la salida:
   - `repaired > 0`: se corrigieron bloques inválidos.
   - `fallback_original > 0`: hubo bloques que no se pudieron retraducir y se dejó original para evitar basura.

## Notas operativas

- Cerrar el navegador no detiene traducciones si el proceso Django sigue vivo.
- Si se detiene el proceso Django durante traducción, el hilo async se corta.
- Para operaciones de reparación masiva, mantén Ollama activo antes de ejecutar auditorías.
