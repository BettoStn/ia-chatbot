# api/index.py
from http.server import BaseHTTPRequestHandler
import json
import os
import base64
from langchain_openai import ChatOpenAI
from langchain_community.utilities import SQLDatabase
from langchain.chains import create_sql_query_chain
from langchain.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser

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
            
            # --- LÓGICA HÍBRIDA PARA RESPUESTAS INTELIGENTES ---
            
            # 1. La IA genera la consulta SQL principal basada en la pregunta del usuario.
            write_query = create_sql_query_chain(llm, db)
            sql_query = write_query.invoke({"question": pregunta})

            # 2. Creamos y ejecutamos una consulta de conteo para saber cuántos registros hay.
            # Esto es más eficiente que traer todos los datos primero.
            count_query = f"SELECT COUNT(*) FROM ({sql_query}) as subquery"
            try:
                count_result = db.run(count_query)
                # El resultado puede ser algo como "[(1500,)]", lo limpiamos para obtener solo el número.
                record_count = int("".join(filter(str.isdigit, count_result)))
            except Exception:
                record_count = 0

            # 3. Decidimos cómo responder basándonos en el conteo de registros.
            
            # CASO A: Pocos registros. Mostramos la tabla en el chat.
            if record_count < 100:
                answer_prompt = PromptTemplate.from_template(
                    """Dada la siguiente pregunta de usuario, la consulta SQL correspondiente y el resultado de la base de datos, proporciona una respuesta amigable y una tabla en formato Markdown si es una lista.
                    Pregunta: {question}
                    Consulta SQL: {query}
                    Resultado SQL: {result}
                    Respuesta:"""
                )
                
                # Ejecutamos la consulta principal para obtener los datos
                result = db.run(sql_query)
                
                # Le pasamos el resultado a la IA para que lo formatee
                chain = (
                    RunnablePassthrough.assign(result=lambda x: result)
                    | answer_prompt
                    | llm
                    | StrOutputParser()
                )
                
                respuesta_final = chain.invoke({"question": pregunta, "query": sql_query})
            
            # CASO B: Muchos registros. Generamos un enlace de descarga.
            else:
                # Codificamos la consulta en base64 para pasarla de forma segura en la URL
                encoded_query = base64.b64encode(sql_query.encode('utf-8')).decode('utf-8')
                download_url = f"https://bodezy.com/vistas/exportar-reporte.php?query={encoded_query}&formato=excel" # ¡Asegúrate que este dominio sea el tuyo!
                
                # Creamos el mensaje de respuesta con el enlace
                respuesta_final = (
                    f"¡Entendido! He encontrado **{record_count} registros**. El resultado es demasiado grande para mostrarlo aquí.\n\n"
                    f"Haz clic en el siguiente enlace para descargar el reporte completo:\n\n"
                    f"📥 [**Descargar Reporte en Excel**]({download_url})"
                )

            # 4. Enviamos la respuesta final al chat.
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"respuesta": respuesta_final}).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
        return