# EPUBDrop

EPUBDrop es una aplicación Django para cargar EPUBs, abrirlos de inmediato y leerlos en formato bilingüe con traducción bajo demanda por bloque usando Ollama.

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
- Lectura inmediata después de subir:
  - el EPUB se procesa y queda disponible apenas se guardan los bloques originales
  - la traducción se genera progresivamente cuando el lector necesita cada bloque
  - si Ollama falla o devuelve una salida inválida, se conserva el HTML original como fallback
- Lector bilingüe en dos columnas:
  - izquierda traducido, derecha original
  - preservación de estructura HTML (títulos, listas, tablas, imágenes, enlaces)
  - scroll, TOC, búsqueda y bookmarks
- Tema visual claro/oscuro y selector persistente.
- Guardado de progreso robusto:
  - guardado remoto en backend
  - respaldo local por libro si falla red
  - indicador visual de estado de guardado
- Endpoint de traducción lazy por bloque.
- Caché de traducción (`TranslationCache`) para evitar retraducciones innecesarias.

## Calidad de traducción y prevención de fallos

El pipeline de traducción lazy incluye controles para evitar respuestas “descriptivas” del modelo (por ejemplo: “No hay contenido HTML para traducir…”):

- Validación estructural de salida traducida:
  - rechaza salida vacía
  - rechaza texto meta/descriptivo/refusal
  - exige conservación de etiquetas estructurales
  - valida preservación de `img[src]` y `a[href]`
- Reintentos inteligentes con prompt reforzado cuando una salida es inválida.
- Si todos los reintentos fallan o la traducción no es válida, usa fallback seguro al HTML original del bloque.
- Validación del caché:
  - si una entrada cacheada resulta inválida, se descarta y se regenera.
- Sanitización/auditoría post-traducción para reparar bloques inválidos sin borrar trabajo correcto.

## Sanitización HTML

EPUBDrop renderiza HTML con `|safe` en el lector, por eso todo contenido HTML pasa por una política explícita de sanitización antes de guardarse o mostrarse.

- El HTML original extraído del EPUB se limpia con Bleach antes de persistirse como bloques.
- Las respuestas de Ollama se sanitizan antes de validarse, guardarse en `TranslationCache` y devolverse al lector.
- Las traducciones ya persistidas se sanitizan de nuevo al renderizar el libro o al responder desde el endpoint lazy.
- Se eliminan scripts, comentarios HTML de respuesta del modelo, atributos de evento como `onclick`/`onerror`, SVG y etiquetas no permitidas.
- Los estilos inline solo se conservan si está disponible el sanitizador CSS de Bleach/tinycss2 y quedan limitados a propiedades permitidas.
- `data:` solo se permite para imágenes embebidas seguras (`png`, `jpg/jpeg`, `gif`, `webp`) en `img[src]`.
- `data:` queda bloqueado en enlaces y `data:image/svg+xml` no se permite.
- Atributos como `width`, `height`, `loading`, `target`, `colspan` y `rowspan` se validan con valores restringidos.

## Flujo de lectura y traducción

1. El usuario sube un EPUB.
2. El backend valida el archivo, extrae metadata/assets y guarda secciones y bloques originales.
3. El libro aparece en la biblioteca y puede abrirse inmediatamente, aunque su estado siga en `TRANSLATING`.
4. El lector muestra dos columnas:
   - izquierda: traducción si ya existe; si no existe, muestra temporalmente el original.
   - derecha: contenido original.
5. El frontend solicita `/api/books/<book_id>/translate-block/<section_idx>/<block_idx>/` solo para bloques cercanos o visibles.
6. El backend reutiliza `translated_html` si ya existe; si no, llama a Ollama, sanitiza y valida la salida, y guarda la traducción válida.
7. `translated_blocks / total_blocks` se actualiza conforme se traducen bloques por demanda.

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
- `reader/views.py`: capa HTTP delgada para biblioteca, lectura y APIs JSON.
- `reader/services.py`: lógica de negocio de biblioteca, upload EPUB, lector, progreso, bookmarks y traducción lazy.
- `reader/tasks.py`: traducción completa/manual por libro usada como soporte operativo.
- `reader/utils.py`: parsing EPUB, traducción con Ollama, validación/sanitización.
- `reader/static/reader/read.js`: lector, progreso, bookmarks, búsqueda y solicitudes de traducción lazy.
- `reader/management/commands/audit_translations.py`: auditoría manual de traducciones.
- `reader/management/commands/restart_translation.py`: reinicio manual de traducción completa para un libro.

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
TRANSLATION_PERFORMANCE_PROFILE=balanced
TRANSLATION_MAX_CONCURRENT_REQUESTS=1
TRANSLATION_REQUEST_COOLDOWN_MS=0
```

### Perfil de rendimiento de traducción

Puedes bajar la carga térmica del equipo regulando cuántas peticiones simultáneas se envían a Ollama y dejando una pausa breve entre bloques:

- `TRANSLATION_PERFORMANCE_PROFILE=eco`
  - limita a 1 petición concurrente
  - agrega una pausa de 350 ms entre peticiones
- `TRANSLATION_PERFORMANCE_PROFILE=balanced`
  - limita a 1 petición concurrente
  - no agrega pausa extra
- `TRANSLATION_PERFORMANCE_PROFILE=max`
  - permite hasta 2 peticiones concurrentes
  - sin pausa extra

Si necesitas afinarlo manualmente, puedes sobreescribir los valores del perfil:

- `TRANSLATION_MAX_CONCURRENT_REQUESTS`: máximo de peticiones simultáneas a Ollama.
- `TRANSLATION_REQUEST_COOLDOWN_MS`: pausa en milisegundos después de cada petición.

Para portátiles con problemas de temperatura, empieza con `eco`.

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

## Operaciones de traducción

El uso normal es traducción lazy por bloque desde el lector. Además existen comandos manuales para mantenimiento o reparación.

### Auditoría de traducciones

La auditoría repara bloques inválidos sin eliminar traducciones válidas.

#### 1) Auditar un libro específico

```bash
.venv/bin/python manage.py audit_translations --book-id <BOOK_UUID>
```

Salida esperada (ejemplo):

```text
[<BOOK_UUID>] scanned=5074 valid=5039 repaired=35 fallback_original=0
TOTAL books=1 scanned=5074 valid=5039 repaired=35 fallback_original=0
```

#### 2) Auditar todos los libros listos

```bash
.venv/bin/python manage.py audit_translations --all-ready
```

#### 3) Flujo recomendado de auditoría

1. Ejecuta auditoría por libro si detectas inconsistencias puntuales.
2. Ejecuta auditoría global (`--all-ready`) después de cambios de prompt/modelo.
3. Revisa campos `repaired` y `fallback_original` en la salida:
   - `repaired > 0`: se corrigieron bloques inválidos.
   - `fallback_original > 0`: hubo bloques que no se pudieron retraducir y se dejó original para evitar basura.

### Reiniciar traducción completa de un libro

El comando `restart_translation` fuerza una retraducción completa de un libro usando el código actual. Es una operación manual de mantenimiento, no el flujo normal de lectura.

```bash
.venv/bin/python manage.py restart_translation --book-id <BOOK_UUID>
```

## Notas operativas

- Cerrar el navegador detiene nuevas solicitudes lazy desde el lector, pero no borra traducciones ya guardadas.
- Un libro puede abrirse mientras está en `TRANSLATING`; ese estado indica progreso de traducción por bloques.
- `READY` significa que todos los bloques registrados ya tienen traducción guardada.
- Para operaciones de reparación masiva, mantén Ollama activo antes de ejecutar auditorías.
