
import os
import time
import json
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
 
app = Flask(__name__)
 
# Configuración - se leen de variables de entorno en Render
INSTAGRAM_USER_ID = os.environ.get("INSTAGRAM_USER_ID")
ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
API_URL = "https://graph.instagram.com/v21.0"
 
# Archivo donde guardamos los videos pendientes de publicar
COLA_FILE = "cola_pendientes.json"
lock = threading.Lock()
 
 
def cargar_cola():
    if not os.path.exists(COLA_FILE):
        return []
    try:
        with open(COLA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []
 
 
def guardar_cola(cola):
    with open(COLA_FILE, "w") as f:
        json.dump(cola, f)
 
 
@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ok", "message": "Instagram webhook server is running"})
 
 
def publicar_en_instagram(video_url, caption):
    """Publica inmediatamente un video en Instagram. Devuelve (exito, mensaje)."""
    try:
        # Paso 1: Crear contenedor
        container_response = requests.post(
            f"{API_URL}/{INSTAGRAM_USER_ID}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": ACCESS_TOKEN
            }
        )
        container_data = container_response.json()
 
        if "error" in container_data:
            return False, container_data["error"]["message"]
 
        container_id = container_data["id"]
 
        # Paso 2: Esperar que procese (hasta 10 min)
        procesado = False
        for i in range(60):
            status_response = requests.get(
                f"{API_URL}/{container_id}",
                params={"fields": "status_code,status", "access_token": ACCESS_TOKEN}
            )
            status_data = status_response.json()
            status = status_data.get("status_code")
 
            if status == "FINISHED":
                procesado = True
                break
            elif status == "ERROR":
                return False, f"Error procesando video: {status_data.get('status')}"
 
            time.sleep(10)
 
        if not procesado:
            return False, "Timeout esperando que Instagram procese el video"
 
        # Paso 3: Publicar
        publish_response = requests.post(
            f"{API_URL}/{INSTAGRAM_USER_ID}/media_publish",
            data={"creation_id": container_id, "access_token": ACCESS_TOKEN}
        )
        publish_data = publish_response.json()
 
        if "error" in publish_data:
            return False, publish_data["error"]["message"]
 
        return True, f"Publicado. Media ID: {publish_data.get('id')}"
 
    except Exception as e:
        return False, str(e)
 
 
@app.route("/upload-instagram", methods=["POST"])
def upload_instagram():
    """
    Recibe:
    {
        "video_url": "https://...",
        "caption": "Texto",
        "publish_at": "2026-06-25T19:00:00Z"  (opcional)
    }
    Si no hay publish_at o ya pasó, publica de inmediato.
    Si hay publish_at futuro, lo guarda en la cola para publicar después.
    """
    data = request.get_json()
    video_url = data.get("video_url")
    caption = data.get("caption", "")
    publish_at = data.get("publish_at")
 
    if not video_url:
        return jsonify({"error": "video_url es requerido"}), 400
 
    debe_publicar_ya = True
    if publish_at:
        try:
            hora_objetivo = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
            ahora = datetime.now(timezone.utc)
            if hora_objetivo > ahora:
                debe_publicar_ya = False
        except Exception:
            pass  # si la fecha viene mal, publicamos de inmediato
 
    if debe_publicar_ya:
        # Publicar en un hilo separado para responder rápido a Zapier
        def hacer():
            exito, msg = publicar_en_instagram(video_url, caption)
            print(f"[Inmediato] Éxito={exito} - {msg}")
        threading.Thread(target=hacer).start()
 
        return jsonify({"success": True, "message": "Publicando de inmediato."})
    else:
        # Guardar en la cola de pendientes
        with lock:
            cola = cargar_cola()
            cola.append({
                "video_url": video_url,
                "caption": caption,
                "publish_at": publish_at
            })
            guardar_cola(cola)
 
        return jsonify({
            "success": True,
            "message": f"Video programado para: {publish_at}"
        })
 
 
@app.route("/revisar-pendientes", methods=["GET", "POST"])
def revisar_pendientes():
    """
    Este endpoint debe ser llamado periódicamente (cada 5-10 min) por un
    servicio externo tipo cron-job.org. Revisa la cola y publica lo que
    ya le toque.
    """
    with lock:
        cola = cargar_cola()
        ahora = datetime.now(timezone.utc)
        pendientes_nuevos = []
        a_publicar = []
 
        for item in cola:
            try:
                hora_objetivo = datetime.fromisoformat(item["publish_at"].replace("Z", "+00:00"))
                if hora_objetivo <= ahora:
                    a_publicar.append(item)
                else:
                    pendientes_nuevos.append(item)
            except Exception:
                # Si la fecha está mal, lo publicamos para no perderlo
                a_publicar.append(item)
 
        guardar_cola(pendientes_nuevos)
 
    resultados = []
    for item in a_publicar:
        def hacer(it=item):
            exito, msg = publicar_en_instagram(it["video_url"], it["caption"])
            print(f"[Programado] Éxito={exito} - {msg}")
        threading.Thread(target=hacer).start()
        resultados.append(item["publish_at"])
 
    return jsonify({
        "revisados": len(cola),
        "publicados_ahora": len(a_publicar),
        "pendientes_restantes": len(cargar_cola()),
        "horas_publicadas": resultados
    })
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
 
