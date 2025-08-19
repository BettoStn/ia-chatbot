from flask import Flask, request, jsonify
import os
import requests
from datetime import date

app = Flask(__name__)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

@app.route("/", methods=["POST"])
def chat():
    try:
        data = request.json
        pregunta = data.get("pregunta", "")

        payload = {
            "model": "deepseek-chat",
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Eres 'Bodezy', un analista de datos experto en SQL. "
                        "Siempre respondes en **español**, claro y conciso. "
                        "Tu tarea es responder preguntas sobre las ventas y la base de datos. "
                        "Si no puedes responder, pide más contexto."
                    )
                },
                {"role": "user", "content": pregunta}
            ]
        }

        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload
        )

        if response.status_code != 200:
            return jsonify({"error": f"Error API DeepSeek: {response.text}"}), 500

        result = response.json()
        respuesta = result["choices"][0]["message"]["content"]

        return jsonify({"respuesta": respuesta})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)
