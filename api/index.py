# api/index.py
from http.server import BaseHTTPRequestHandler
import json
import os
from langchain_openai import ChatOpenAI
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

class handler(BaseHTTPRequestHandler):
    
    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200, "ok")
        self.send_cors_headers()
        self.end_headers()

    def do_POST(self):
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            body = json.loads(post_data)
            pregunta = body.get('pregunta', '')

            if not pregunta:
                # ... (código de manejo de error sin cambios)
                return

            # --- CONFIGURACIÓN ---
            api_key = os.environ.get("OPENAI_API_KEY")
            db_uri = os.environ.get("DATABASE_URI")

            llm = ChatOpenAI(model="gpt-4o", openai_api_key=api_key, temperature=0)
            db = SQLDatabase.from_uri(db_uri)
            
            # --- CREACIÓN DEL AGENTE DE SQL ---
            # Esta es la nueva lógica mejorada
            agent_executor = create_sql_agent(llm, db=db, agent_type="openai-tools", verbose=True)
            
            # Invocamos al agente con la pregunta
            resultado_agente = agent_executor.invoke({"input": pregunta})
            
            # El agente devuelve la respuesta final en el campo 'output'
            respuesta_final = resultado_agente.get("output", "No se pudo obtener una respuesta.")

            # Envía la respuesta de vuelta
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"respuesta": respuesta_final}).encode())

        except Exception as e:
            # ... (código de manejo de error sin cambios)
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
        return