import os
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Configuración - se leen de variables de entorno en Render
INSTAGRAM_USER_ID = os.environ.get("INSTAGRAM_USER_ID")
ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
API_URL = "https://graph.instagram.com/v21.0"


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ok", "message": "Instagram webhook server is running"})


@app.route("/upload-instagram", methods=["POST"])
def upload_instagram():
    """
    Recibe un webhook con:
    {
        "video_url": "https://...",  (link público del video, ej: de Google Drive)
        "caption": "Texto del caption"
    }
    Y lo publica en Instagram como Reel.
    """
    try:
        data = request.get_json()
        video_url = data.get("video_url")
        caption = data.get("caption", "")

        if not video_url:
            return jsonify({"error": "video_url es requerido"}), 400

        # Paso 1: Crear contenedor en Instagram
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
            return jsonify({"error": container_data["error"]["message"]}), 400

        container_id = container_data["id"]

        # Paso 2: Esperar a que Instagram procese el video
        procesado = False
        for i in range(30):
            status_response = requests.get(
                f"{API_URL}/{container_id}",
                params={
                    "fields": "status_code,status",
                    "access_token": ACCESS_TOKEN
                }
            )
            status_data = status_response.json()
            status = status_data.get("status_code")

            if status == "FINISHED":
                procesado = True
                break
            elif status == "ERROR":
                return jsonify({"error": f"Error procesando video: {status_data.get('status')}"}), 400

            time.sleep(10)

        if not procesado:
            return jsonify({"error": "Timeout esperando que Instagram procese el video"}), 400

        # Paso 3: Publicar
        publish_response = requests.post(
            f"{API_URL}/{INSTAGRAM_USER_ID}/media_publish",
            data={
                "creation_id": container_id,
                "access_token": ACCESS_TOKEN
            }
        )
        publish_data = publish_response.json()

        if "error" in publish_data:
            return jsonify({"error": publish_data["error"]["message"]}), 400

        return jsonify({
            "success": True,
            "media_id": publish_data.get("id"),
            "message": "Video publicado en Instagram exitosamente"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
