# api/index.py
from http.server import BaseHTTPRequestHandler
import json
import os
from langchain_openai import ChatOpenAI
from langchain_community.utilities import SQLDatabase
from langchain.chains import create_sql_query_chain

class handler(BaseHTTPRequestHandler):
    
    def send_cors_headers(self):
        """Envía las cabeceras para permitir peticiones desde cualquier origen (CORS)"""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        """Responde a las peticiones de 'inspección' (preflight) del navegador"""
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
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No se proporcionó ninguna pregunta."}).encode())
                return

            # --- CONFIGURACIÓN ---
            api_key = os.environ.get("OPENAI_API_KEY")
            db_uri = os.environ.get("DATABASE_URI")

            llm = ChatOpenAI(model="gpt-4o", openai_api_key=api_key, temperature=0)
            db = SQLDatabase.from_uri(db_uri)
            
            write_query_chain = create_sql_query_chain(llm, db)
            query_sql = write_query_chain.invoke({"question": pregunta})

            resultado = db.run(query_sql)

            # Envía la respuesta de vuelta
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_cors_headers() # <--- AÑADIDO IMPORTANTE
            self.end_headers()
            self.wfile.write(json.dumps({"respuesta": resultado}).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.send_cors_headers() # <--- AÑADIDO IMPORTANTE
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

        return