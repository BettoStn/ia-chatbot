# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import base64
import re # Importamos la librería de expresiones regulares para la validación
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

app = Flask(__name__)
CORS(app)

# --- NUEVO: El "Guardia de Seguridad" ---
def validar_sql(sql_query: str, empresa_id: int):
    """
    Esta función valida el SQL generado por la IA.
    Devuelve True si es seguro, False si no lo es.
    """
    lower_sql = sql_query.lower().strip()

    # Regla 1: Solo permitir consultas SELECT.
    if not lower_sql.startswith('select'):
        print(f"VALIDATION FAILED: Not a SELECT statement. Query: {sql_query}")
        return False

    # Regla 2: Prohibir modificaciones (doble chequeo).
    forbidden_keywords = ['update', 'delete', 'insert', 'drop', 'alter', 'truncate']
    if any(keyword in lower_sql for keyword in forbidden_keywords):
        print(f"VALIDATION FAILED: Contains forbidden keywords. Query: {sql_query}")
        return False
        
    # Regla 3: Asegurarse de que SIEMPRE filtre por el empresa_id del usuario.
    # Buscamos patrones como "empresa_id = 9" o "empresas.id = 9"
    empresa_filter_pattern = re.compile(r"empresa_id\s*=\s*" + str(empresa_id))
    empresa_id_pattern = re.compile(r"empresas\.id\s*=\s*" + str(empresa_id))

    if not empresa_filter_pattern.search(lower_sql) and not empresa_id_pattern.search(lower_sql):
         print(f"VALIDATION FAILED: Missing correct empresa_id filter. Query: {sql_query}")
         return False

    # Regla 4: Asegurarse de que NO contenga un empresa_id diferente.
    # Buscamos "empresa_id = OTRO_NUMERO"
    all_empresa_ids = re.findall(r'empresa_id\s*=\s*(\d+)', lower_sql)
    for eid in all_empresa_ids:
        if int(eid) != empresa_id:
            print(f"VALIDATION FAILED: Attempted to access forbidden empresa_id={eid}. Query: {sql_query}")
            return False

    return True # Si pasa todas las reglas, la consulta es segura.


@app.route('/', methods=['POST', 'OPTIONS'])
def handle_query():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        body = request.get_json()
        prompt_completo = body.get('pregunta', '')
        empresa_id_from_prompt = int(re.search(r'empresa_id = (\d+)', prompt_completo).group(1))

        if not prompt_completo:
            return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

        api_key = os.environ.get("GOOGLE_API_KEY")
        db_uri = os.environ.get("DATABASE_URI")
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", google_api_key=api_key, temperature=0)
        db = SQLDatabase.from_uri(db_uri)
        
        agent_executor = create_sql_agent(llm, db=db, agent_type="openai-tools", verbose=True)
        resultado_agente = agent_executor.invoke({"input": prompt_completo})
        
        # Extraemos el SQL generado por el agente (si lo hay)
        # Esto requiere un poco de análisis del texto de 'log' que el agente produce
        intermediate_steps = resultado_agente.get("intermediate_steps", [])
        sql_query_generada = ""
        if intermediate_steps and "sql_db_query" in str(intermediate_steps):
            # Extraemos el query del log para validarlo
            match = re.search(r"sql_db_query`:\s*`(.+?)`", str(intermediate_steps), re.DOTALL)
            if match:
                sql_query_generada = match.group(1)

        # --- Ejecutamos la validación del "Guardia de Seguridad" ---
        if sql_query_generada and not validar_sql(sql_query_generada, empresa_id_from_prompt):
             # Si la validación falla, devolvemos un error de acceso denegado.
             respuesta_final = "Lo siento, no tengo permiso para realizar esa consulta."
        else:
             # Si la validación pasa (o no hubo SQL), usamos la respuesta normal del agente.
             respuesta_final = resultado_agente.get("output", "No se pudo obtener una respuesta.")

        return jsonify({"respuesta": respuesta_final})

    except Exception as e:
        print(f"Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))