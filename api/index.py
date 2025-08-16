# api/index.py
from flask import Flask, request, jsonify
from flask_cors import CORS # La librería se importa correctamente
import os
import base64
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.utilities import SQLDatabase
from langchain.chains import create_sql_query_chain
from langchain.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser

# Configuración del servidor Flask
app = Flask(__name__)
CORS(app) # **ESTA ES LA LÍNEA QUE FALTABA Y SOLUCIONA TODO**

@app.route('/api', methods=['POST'])
def handle_query():
    try:
        body = request.get_json()
        pregunta = body.get('pregunta', '')

        if not pregunta:
            return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

        # --- CONFIGURACIÓN DE LA IA ---
        api_key = os.environ.get("GOOGLE_API_KEY")
        db_uri = os.environ.get("DATABASE_URI")

        llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", google_api_key=api_key, temperature=0)
        db = SQLDatabase.from_uri(db_uri)
        
        # --- LÓGICA HÍBRIDA ---
        write_query = create_sql_query_chain(llm, db)
        raw_sql_output = write_query.invoke({"question": pregunta})

        sql_query = ""
        select_pos = raw_sql_output.upper().find("SELECT")
        if select_pos != -1:
            sql_query = raw_sql_output[select_pos:]
            if sql_query.strip().endswith("```"):
                sql_query = sql_query.strip()[:-3].strip()

        if not sql_query:
            respuesta_final = raw_sql_output
        else:
            count_query = f"SELECT COUNT(*) FROM ({sql_query}) as subquery"
            try:
                count_result = db.run(count_query)
                record_count = int("".join(filter(str.isdigit, count_result)))
            except Exception:
                record_count = 0

            if record_count < 100:
                answer_prompt = PromptTemplate.from_template(
                    """Dada la siguiente pregunta, consulta SQL y resultado, proporciona una respuesta amigable y una tabla en formato Markdown si aplica.
                    Pregunta: {question}, Consulta SQL: {query}, Resultado SQL: {result}, Respuesta:"""
                )
                result = db.run(sql_query)
                chain = (RunnablePassthrough.assign(result=lambda x: result) | answer_prompt | llm | StrOutputParser())
                respuesta_final = chain.invoke({"question": pregunta, "query": sql_query})
            else:
                encoded_query = base64.b64encode(sql_query.encode('utf-8')).decode('utf-8')
                download_url = f"[https://bodezy.com/vistas/exportar-reporte.php?query=](https://bodezy.com/vistas/exportar-reporte.php?query=){encoded_query}&formato=excel"
                respuesta_final = (f"¡Entendido! He encontrado **{record_count} registros**. El resultado es demasiado grande para mostrarlo aquí.\n\n"
                                 f"Haz clic en el siguiente enlace para descargar el reporte completo:\n\n"
                                 f"📥 [**Descargar Reporte en Excel**]({download_url})")

        return jsonify({"respuesta": respuesta_final})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))