# Free News RSS Bot

This project:
1. Searches Google News RSS.
2. Finds the publisher's original URL.
3. Downloads the article page.
4. Extracts the readable article body with Trafilatura.
5. Saves articles in `data/articles.json`.
6. Creates `docs/feed.xml`, including the extracted body in `content:encoded`.
7. Creates a simple `docs/index.html` dashboard.

## First local run on Windows

Install Python 3.12 from python.org and select "Add python.exe to PATH" during setup.

Open this folder in File Explorer. Click the address bar, type `cmd`, and press Enter.

Run:

    py -m venv .venv
    .venv\Scripts\activate
    py -m pip install --upgrade pip
    py -m pip install -r requirements.txt
    py main.py

Then start a local web server:

    py -m http.server 8000 --directory docs

Open:

    http://localhost:8000/
    http://localhost:8000/feed.xml

Stop the server with Ctrl+C.

## Change the searches

Edit `config.json`.

Example:

    "queries": [
      "Los Angeles Dodgers",
      "Shohei Ohtani",
      "Aaron Judge Yankees"
    ]

Run `py main.py` again after making changes.

## Important

Some websites block automated access, use paywalls, or render their article body with JavaScript. Those stories may be skipped. This is normal.

If the repository is PUBLIC, anything committed to it can be public, including extracted article bodies. Keep the repository PRIVATE if the full bodies are only for internal use.


## Configuración incluida en esta versión

Esta versión ya viene configurada con 52 búsquedas para LMB, LMP, equipos, jugadores y temas relacionados.

- Región de Google News: México
- Idioma: español
- Ventana inicial: últimas 48 horas
- Hasta 10 resultados revisados por búsqueda
- Hasta 1,000 artículos almacenados
- Hasta 150 artículos en el RSS

# Monitor de Noticias LMB y LMP — v2

Cambios principales:

- Detecta noticias repetidas de forma conservadora.
- Si dos notas parecen contar la misma noticia, conserva la que tenga el cuerpo extraído más largo.
- Une las búsquedas que encontraron la misma noticia.
- Conserva una lista interna de fuentes duplicadas.
- El título del RSS ahora usa: `Titular | Fuente`.
- Ejemplo: `Trevor Bauer habla de su futuro | ESPN`.

## Archivos que normalmente necesitas reemplazar en GitHub

Si ya tienes el proyecto funcionando, reemplaza:
- `main.py`

No necesitas cambiar `config.json` ni `update.yml` para obtener estas dos mejoras.

## Si estás subiendo el proyecto completo

Asegúrate de que estos archivos estén en la raíz:
- `main.py`
- `config.json`
- `requirements.txt`

Y que el workflow esté exactamente en:
- `.github/workflows/update.yml`


## Version 3: exclude albat.com

- Articles from `albat.com` are completely excluded.
- Any `*.albat.com` subdomain is also excluded.
- Existing Al Bat articles already stored in `data/articles.json` are automatically removed on the next run.
