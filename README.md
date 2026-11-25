# MyRecorder

Aplicación local para grabar directos de YouTube en un único archivo MKV. Permite iniciar, pausar, reanudar y detener capturas; consultar la duración, ver la portada del video, abrir su carpeta y eliminar grabaciones finalizadas.

## Requisitos

- Windows 10/11
- Python 3.10 o posterior
- FFmpeg disponible en `PATH`

## Instalación y ejecución

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

Abre http://127.0.0.1:8765 en el navegador. Pega la URL de un directo activo para comenzar a grabar.

Las grabaciones se guardan por defecto en `recordings` dentro del proyecto. Para usar otra ubicación, define la variable de entorno `MYRECORDER_OUTPUT` antes de iniciar la aplicación.

FFmpeg guarda el video y el audio en un archivo MKV, codificando el audio como AAC para mejorar la compatibilidad con reproductores. Al finalizar una captura, MyRecorder crea una portada JPG a partir de un fotograma del video. Las grabaciones finalizadas y su estado se guardan localmente.

## Desarrollo

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8765 --reload
```

