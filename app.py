# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

# Configuración del servidor Flask
app = Flask(__name__)
CORS(app)

# --- RUTA PRINCIPAL ---
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
        api_key = os.environ.get("GOOGLE_API_KEY")
        db_uri = os.environ.get("DATABASE_URI")

        llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", google_api_key=api_key, temperature=0)
        db = SQLDatabase.from_uri(db_uri)
        
        # --- CREACIÓN Y USO DEL AGENTE DE SQL ---
        # El Agente es más inteligente para decidir si debe usar la BD o solo conversar.
        agent_executor = create_sql_agent(
            llm,
            db=db,
            agent_type="openai-tools",
            verbose=True,
            # Se puede añadir un prefijo al prompt del agente si es necesario,
            # pero el prompt principal del usuario ya contiene el contexto.
        )
        
        # Invocamos al agente con el prompt completo que viene desde el frontend
        resultado_agente = agent_executor.invoke({"input": prompt_completo})
        
        # El agente devuelve la respuesta final y natural en el campo 'output'
        respuesta_final = resultado_agente.get("output", "No se pudo obtener una respuesta.")

        return jsonify({"respuesta": respuesta_final})

    except Exception as e:
        # Imprime el error en los logs de Render para depuración
        print(f"Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))