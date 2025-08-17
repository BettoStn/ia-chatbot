# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import base64
# CAMBIO 1: Importamos la librería de DeepSeek
from langchain_deepseek import ChatDeepSeek
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

# Configuración del servidor Flask
app = Flask(__name__)
CORS(app)

@app.route('/', methods=['POST', 'OPTIONS'])
def handle_query():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        body = request.get_json()
        prompt_completo = body.get('pregunta', '')

        if not prompt_completo:
            return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

        # --- CONFIGURACIÓN DE LA IA ---
        # CAMBIO 2: Ahora buscamos la clave de API de DeepSeek
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        db_uri = os.environ.get("DATABASE_URI")

        # CAMBIO 3: Inicializamos el modelo de DeepSeek
        llm = ChatDeepSeek(model="deepseek-chat", deepseek_api_key=api_key, temperature=0)
        
        db = SQLDatabase.from_uri(db_uri)
        
        # --- CREACIÓN Y USO DEL AGENTE DE SQL (Esta parte no cambia) ---
        agent_executor = create_sql_agent(
            llm,
            db=db,
            agent_type="openai-tools",
            verbose=True,
        )
        
        resultado_agente = agent_executor.invoke({"input": prompt_completo})
        
        respuesta_final = resultado_agente.get("output", "No se pudo obtener una respuesta.")

        return jsonify({"respuesta": respuesta_final})

    except Exception as e:
        print(f"Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))