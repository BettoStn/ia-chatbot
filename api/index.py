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
            
            # 1. La IA genera la consulta SQL (puede incluir texto extra).
            write_query = create_sql_query_chain(llm, db)
            raw_sql_output = write_query.invoke({"question": pregunta})

            # --- **NUEVA LÓGICA DE LIMPIEZA** ---
            # Extraemos solo el código SQL puro, ignorando cualquier texto previo.
            sql_query = ""
            # Buscamos la primera aparición de "SELECT" (insensible a mayúsculas/minúsculas)
            select_pos = raw_sql_output.upper().find("SELECT")
            if select_pos != -1:
                # Si encontramos "SELECT", tomamos el string desde ahí hasta el final.
                sql_query = raw_sql_output[select_pos:]
                # A veces la IA termina el código con ```, lo quitamos.
                if sql_query.strip().endswith("```"):
                    sql_query = sql_query.strip()[:-3].strip()
            # --- FIN DE LA LÓGICA DE LIMPIEZA ---

            # Si después de limpiar no tenemos una consulta SQL, es una respuesta conversacional.
            if not sql_query:
                respuesta_final = raw_sql_output
            else:
                # Si tenemos una consulta, continuamos con la lógica de conteo.
                count_query = f"SELECT COUNT(*) FROM ({sql_query}) as subquery"
                try:
                    count_result = db.run(count_query)
                    record_count = int("".join(filter(str.isdigit, count_result)))
                except Exception:
                    record_count = 0

                # Decidimos la respuesta basada en el conteo.
                if record_count < 100:
                    answer_prompt = PromptTemplate.from_template(
                        """Dada la siguiente pregunta, consulta SQL y resultado, proporciona una respuesta amigable y una tabla en Markdown si aplica.
                        Pregunta: {question}
                        Consulta SQL: {query}
                        Resultado SQL: {result}
                        Respuesta:"""
                    )
                    result = db.run(sql_query)
                    chain = (
                        RunnablePassthrough.assign(result=lambda x: result)
                        | answer_prompt
                        | llm
                        | StrOutputParser()
                    )
                    respuesta_final = chain.invoke({"question": pregunta, "query": sql_query})
                else:
                    encoded_query = base64.b64encode(sql_query.encode('utf-8')).decode('utf-8')
                    download_url = f"https://bodezy.com/vistas/exportar-reporte.php?query={encoded_query}&formato=excel"
                    respuesta_final = (
                        f"¡Entendido! He encontrado **{record_count} registros**. El resultado es demasiado grande para mostrarlo aquí.\n\n"
                        f"Haz clic en el siguiente enlace para descargar el reporte completo:\n\n"
                        f"📥 [**Descargar Reporte en Excel**]({download_url})"
                    )

            # Enviamos la respuesta final.
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