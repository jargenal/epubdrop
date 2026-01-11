# EPUBDrop (Django) — Biblioteca bilingue con traduccion persistente

Proyecto en Django 4.2.27 para **subir EPUBs**, **extraer contenido enriquecido**, y **leerlos en un lector bilingue** con **traduccion local gratuita** usando **LibreTranslate**.  
El lector muestra **Original (derecha)** y **Traduccion (izquierda)**, alineado **por bloque/parrafo** (grid 2 columnas).

---

## 1) Funcionalidades actuales

### ✅ Implementado
- **Biblioteca estilo Kindle** (pagina de inicio):
  - lista de libros listos
  - seccion de carga integrada
  - buscador por titulo o autor
  - estado y progreso de traduccion
- **Upload de EPUB** (drag & drop) + validacion:
  - extension `.epub`
  - validacion basica de MIME y cabecera ZIP
- **Guardado en disco** del EPUB y assets extraidos
- **Persistencia en base de datos**:
  - libros, metadata, secciones y bloques
  - traducciones por bloque
  - progreso de lectura por usuario
- **Traduccion async** por bloques (background thread)
- **Notificacion por email** al finalizar traduccion (o fallo)
- **Lector bilingue (read.html)**:
  - 2 columnas: traduccion / original
  - alineacion por bloque/parrafo con CSS Grid
  - tipografia configurable (familia y tamanio)
  - lectura limpia (sin tarjetas, sin redondeos)
- **Progreso de lectura persistente**:
  - guarda el bloque actual y offset dentro del parrafo
  - restaura el punto exacto al volver
- **Boton Limpiar** (borra disco + DB del libro)

### 🔜 Pendiente / futuro
- Navegacion por TOC
- Busqueda dentro del libro (original/traduccion)
- Bookmarks y notas
- Batch translation con cola (Celery/RQ)

---

## 2) Stack tecnico

- **Python**: 3.9.x
- **Django**: 4.2.27
- **EPUB parsing**: ebooklib
- **HTML parsing / normalizacion**: lxml + bleach
- **Frontend**: Tailwind CDN
- **Traduccion local**: LibreTranslate (endpoint local)
- **Assets**: `MEDIA_ROOT` + `MEDIA_URL`

---

## 3) Estructura de proyecto (referencial)

- `config/` -> settings principales
- `reader/` -> app principal
  - `models.py` -> Book, Section, Block, ReadingProgress, CustomUser
  - `views.py` -> upload, lectura, progreso, limpieza
  - `tasks.py` -> traduccion async + email
  - `templates/reader/` -> biblioteca y lector
- `media/` -> EPUBs y assets extraidos

---

## 4) Configuracion rapida

1) Crear entorno virtual e instalar dependencias:
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Variables de entorno (ejemplo en `.env.example`):
- `LIBRETRANSLATE_URL`
- `EMAIL_*` para SMTP

3) Migraciones:
```
python3 manage.py migrate
```

4) Crear superusuario:
```
python3 manage.py createsuperuser
```

5) Ejecutar servidor:
```
python3 manage.py runserver
```

---

## 5) Notas importantes

- Autenticacion por **email** (CustomUser) como `USERNAME_FIELD`.
- Progreso de lectura se guarda por usuario (no por session).
- Si cambias el user model en un proyecto ya existente, se recomienda partir de una DB limpia.

---

## 6) LibreTranslate (local)

Ejemplo con Docker:
```
docker run -d -p 5050:5000 libretranslate/libretranslate
```
Luego en `.env`:
```
LIBRETRANSLATE_URL=http://127.0.0.1:5050/translate
```
