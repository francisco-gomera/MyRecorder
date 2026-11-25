# MyRecorder

Aplicación local para grabar directos de YouTube en un único archivo MKV. Permite iniciar, pausar, reanudar y detener capturas; consultar la duración, ver la portada del video, abrir su carpeta y eliminar grabaciones finalizadas.

## Requisitos

- Windows 10/11
- Python 3.10 o posterior
- FFmpeg y ffprobe disponibles en `PATH` (ffprobe se instala junto con FFmpeg)

## Instalación y ejecución

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

Abre http://127.0.0.1:8765 en el navegador. Pega la URL de un directo activo para comenzar a grabar.

Las grabaciones se guardan por defecto en `recordings` dentro del proyecto. Para usar otra ubicación, define la variable de entorno `MYRECORDER_OUTPUT` antes de iniciar la aplicación.

FFmpeg guarda el video y el audio en un archivo MKV, codificando el audio como AAC para mejorar la compatibilidad con reproductores. MyRecorder prefiere formatos muxed de YouTube, que llevan audio y video en el mismo stream; usa streams separados solo si no hay un formato muxed disponible. Al finalizar una captura, crea una portada JPG a partir de un fotograma del video. Las grabaciones finalizadas y su estado se guardan localmente.

Durante la captura, MyRecorder guarda partes MKV internas. Pausar cierra la parte activa; reanudar resuelve de nuevo el directo y comienza otra parte. Si FFmpeg se interrumpe inesperadamente y el directo sigue activo, MyRecorder intenta reconectar hasta tres veces. Al detener o finalizar la captura, une las partes en un único MKV para la galería.

### Diagnosticar una grabación sin audio

Cada carpeta de grabación contiene:

- `formats.txt`: tipo de selección (`muxed`, `pair` o `unknown`) y metadatos de los formatos seleccionados. No incluye URLs firmadas ni temporales.
- `ffmpeg.log`: advertencias y errores del proceso FFmpeg.

Si ffprobe detecta que el archivo no tiene pista de audio, la tarjeta de la galería muestra un aviso. Revisa `formats.txt` para ver qué streams eligió yt-dlp y `ffmpeg.log` para encontrar errores al abrir o procesar esos streams. `has_audio` indica el resultado de ffprobe en el estado de la grabación.

## Desarrollo

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8765 --reload
```

Instala las dependencias de desarrollo y ejecuta los tests de formatos y argumentos FFmpeg:

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

