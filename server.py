import os
import time
import json
import threading
import subprocess
import tempfile
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

# ─── Configuración Instagram ───────────────────────────────────────────────
INSTAGRAM_USER_ID = os.environ.get("INSTAGRAM_USER_ID")
INSTAGRAM_TOKEN   = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
IG_API            = "https://graph.instagram.com/v21.0"

# ─── Configuración YouTube ────────────────────────────────────────────────
YT_CLIENT_ID      = os.environ.get("YT_CLIENT_ID")
YT_CLIENT_SECRET  = os.environ.get("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN  = os.environ.get("YT_REFRESH_TOKEN")

# ─── Cola de pendientes de Instagram ────────────────────────────────────
COLA_FILE = "cola_pendientes.json"
lock = threading.Lock()


# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════

def cargar_cola():
    if not os.path.exists(COLA_FILE):
        return []
    try:
        with open(COLA_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def guardar_cola(cola):
    with open(COLA_FILE, "w") as f:
        json.dump(cola, f)

def descargar_video(url, suffix=".mp4"):
    """Descarga un video desde una URL y devuelve la ruta temporal."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    r = requests.get(url, stream=True, timeout=300)
    r.raise_for_status()
    for chunk in r.iter_content(chunk_size=1024*1024):
        tmp.write(chunk)
    tmp.close()
    return tmp.name

def convertir_a_vertical(input_path):
    """
    Usa FFmpeg para convertir el video a 9:16 con barras negras si es necesario.
    Devuelve la ruta del archivo convertido.
    """
    output_path = input_path.replace(".mp4", "_vertical.mp4")
    
    # Filtro: escala el video para que quepa en 1080x1920 manteniendo proporción,
    # luego agrega barras negras donde haga falta (pad)
    filtro = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", filtro,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-movflags", "+faststart",
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"FFmpeg error: {result.stderr}")
    
    return output_path

def get_youtube_service():
    """Obtiene el servicio de YouTube usando OAuth2 con refresh token."""
    creds = Credentials(
        token=None,
        refresh_token=YT_REFRESH_TOKEN,
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


# ════════════════════════════════════════════════════════════════════════════
# INSTAGRAM
# ════════════════════════════════════════════════════════════════════════════

def publicar_en_instagram(video_url, caption):
    """Publica en Instagram. Devuelve (exito, mensaje)."""
    try:
        container_response = requests.post(
            f"{IG_API}/{INSTAGRAM_USER_ID}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": INSTAGRAM_TOKEN
            }
        )
        container_data = container_response.json()
        if "error" in container_data:
            return False, container_data["error"]["message"]

        container_id = container_data["id"]
        procesado = False
        for _ in range(60):
            s = requests.get(
                f"{IG_API}/{container_id}",
                params={"fields": "status_code,status", "access_token": INSTAGRAM_TOKEN}
            ).json()
            if s.get("status_code") == "FINISHED":
                procesado = True
                break
            elif s.get("status_code") == "ERROR":
                return False, f"Error: {s.get('status')}"
            time.sleep(10)

        if not procesado:
            return False, "Timeout procesando video"

        pub = requests.post(
            f"{IG_API}/{INSTAGRAM_USER_ID}/media_publish",
            data={"creation_id": container_id, "access_token": INSTAGRAM_TOKEN}
        ).json()

        if "error" in pub:
            return False, pub["error"]["message"]

        return True, f"Publicado. ID: {pub.get('id')}"
    except Exception as e:
        return False, str(e)

def procesar_instagram(video_url, caption, publish_at=None):
    try:
        if publish_at:
            hora_obj = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
            segundos = (hora_obj - datetime.now(timezone.utc)).total_seconds()
            if segundos > 0:
                time.sleep(segundos)
        exito, msg = publicar_en_instagram(video_url, caption)
        print(f"[Instagram] exito={exito} {msg}")
    except Exception as e:
        print(f"[Instagram] Error: {e}")


# ════════════════════════════════════════════════════════════════════════════
# YOUTUBE
# ════════════════════════════════════════════════════════════════════════════

def subir_a_youtube(video_path, titulo, descripcion, privacy_status, publish_at=None):
    """Sube un video local a YouTube."""
    youtube = get_youtube_service()

    body = {
        "snippet": {
            "title": titulo[:100],
            "description": descripcion,
            "categoryId": "22",
            "tags": ["Shorts"]
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        }
    }

    if publish_at and privacy_status == "private":
        body["status"]["publishAt"] = publish_at

    media = MediaFileUpload(video_path, chunksize=1024*1024, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = req.next_chunk()

    return response["id"]

def procesar_youtube(drive_url, titulo, descripcion, privacy_status, publish_at=None):
    """Descarga, convierte a vertical y sube a YouTube."""
    input_path = None
    output_path = None
    try:
        print(f"[YouTube] Descargando video...")
        input_path = descargar_video(drive_url)

        print(f"[YouTube] Convirtiendo a 9:16 vertical...")
        output_path = convertir_a_vertical(input_path)

        print(f"[YouTube] Subiendo a YouTube...")
        video_id = subir_a_youtube(output_path, titulo, descripcion, privacy_status, publish_at)
        print(f"[YouTube] ¡Publicado! ID: {video_id} — https://youtube.com/shorts/{video_id}")

    except Exception as e:
        print(f"[YouTube] Error: {e}")
    finally:
        for p in [input_path, output_path]:
            if p and os.path.exists(p):
                os.remove(p)


# ════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ok", "message": "VideoUploader server running"})


@app.route("/upload-instagram", methods=["POST"])
def upload_instagram():
    data = request.get_json()
    video_url  = data.get("video_url")
    caption    = data.get("caption", "")
    publish_at = data.get("publish_at")

    if not video_url:
        return jsonify({"error": "video_url es requerido"}), 400

    debe_publicar_ya = True
    if publish_at:
        try:
            hora_obj = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
            if hora_obj > datetime.now(timezone.utc):
                debe_publicar_ya = False
        except Exception:
            pass

    if debe_publicar_ya:
        threading.Thread(target=procesar_instagram, args=(video_url, caption)).start()
        return jsonify({"success": True, "message": "Publicando en Instagram de inmediato."})
    else:
        with lock:
            cola = cargar_cola()
            cola.append({"video_url": video_url, "caption": caption, "publish_at": publish_at})
            guardar_cola(cola)
        return jsonify({"success": True, "message": f"Instagram programado para: {publish_at}"})


@app.route("/upload-youtube", methods=["POST"])
def upload_youtube():
    """
    Recibe:
    {
        "drive_url": "https://drive.google.com/uc?...",
        "titulo": "Título del video",
        "descripcion": "Caption completo",
        "privacy_status": "public" o "private",
        "publish_at": "2026-06-25T19:00:00Z"  (opcional)
    }
    Descarga el video, lo convierte a 9:16 y lo sube a YouTube.
    """
    data = request.get_json()
    drive_url      = data.get("drive_url")
    titulo         = data.get("titulo", "")
    descripcion    = data.get("descripcion", "")
    privacy_status = data.get("privacy_status", "public")
    publish_at     = data.get("publish_at")

    if not drive_url:
        return jsonify({"error": "drive_url es requerido"}), 400

    threading.Thread(
        target=procesar_youtube,
        args=(drive_url, titulo, descripcion, privacy_status, publish_at)
    ).start()

    return jsonify({"success": True, "message": "Procesando subida a YouTube en segundo plano."})


@app.route("/revisar-pendientes", methods=["GET", "POST"])
def revisar_pendientes():
    with lock:
        cola = cargar_cola()
        ahora = datetime.now(timezone.utc)
        pendientes_nuevos = []
        a_publicar = []
        for item in cola:
            try:
                hora_obj = datetime.fromisoformat(item["publish_at"].replace("Z", "+00:00"))
                if hora_obj <= ahora:
                    a_publicar.append(item)
                else:
                    pendientes_nuevos.append(item)
            except Exception:
                a_publicar.append(item)
        guardar_cola(pendientes_nuevos)

    for item in a_publicar:
        threading.Thread(
            target=procesar_instagram,
            args=(item["video_url"], item["caption"])
        ).start()

    return jsonify({
        "publicados_ahora": len(a_publicar),
        "pendientes_restantes": len(pendientes_nuevos)
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
