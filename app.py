# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import base64
import re # Importamos la librería de expresiones regulares para la validación
from langchain_deepseek import ChatDeepSeek
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

app = Flask(__name__)
CORS(app)

# --- EL "GUARDIA DE SEGURIDAD" ---
def validar_sql(sql_query: str, empresa_id: int):
    """
    Esta función valida el SQL generado por la IA para máxima seguridad.
    Devuelve True si es seguro, False si no lo es.
    """
    lower_sql = sql_query.lower().strip()

    # Regla 1: Solo permitir consultas SELECT.
    if not lower_sql.startswith('select'):
        print(f"VALIDATION FAILED: Not a SELECT statement. Query: {sql_query}")
        return False

    # Regla 2: Prohibir palabras clave de modificación de datos.
    forbidden_keywords = ['update', 'delete', 'insert', 'drop', 'alter', 'truncate', 'grant', 'revoke']
    if any(keyword in lower_sql for keyword in forbidden_keywords):
        print(f"VALIDATION FAILED: Contains forbidden keywords. Query: {sql_query}")
        return False
        
    # Regla 3: Si la consulta no es a la tabla 'empresas', DEBE contener el filtro del empresa_id del usuario.
    if 'from empresas' not in lower_sql:
        empresa_filter_pattern = re.compile(r"empresa_id\s*=\s*" + str(empresa_id))
        if not empresa_filter_pattern.search(lower_sql):
            print(f"VALIDATION FAILED: Missing correct empresa_id filter. Query: {sql_query}")
            return False

    # Regla 4: Prohibir explícitamente que se consulte un empresa_id diferente.
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
        
        if not prompt_completo:
            return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

        # Extraemos el ID de la empresa del prompt para la validación
        empresa_id_match = re.search(r'empresa_id = (\d+)', prompt_completo)
        if not empresa_id_match:
            return jsonify({"error": "No se pudo determinar el ID de la empresa para la validación."}), 400
        empresa_id_from_prompt = int(empresa_id_match.group(1))

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        db_uri = os.environ.get("DATABASE_URI")
        llm = ChatDeepSeek(model="deepseek-chat", api_key=api_key, temperature=0)
        db = SQLDatabase.from_uri(db_uri)
        
        agent_executor = create_sql_agent(llm, db=db, agent_type="openai-tools", verbose=True)
        resultado_agente = agent_executor.invoke({"input": prompt_completo})
        
        intermediate_steps = resultado_agente.get("intermediate_steps", [])
        sql_query_generada = ""
        # Buscamos en los pasos intermedios la consulta SQL que el agente generó
        if intermediate_steps:
            tool_calls = intermediate_steps[0]
            if tool_calls and hasattr(tool_calls[0], 'tool_input') and isinstance(tool_calls[0].tool_input, dict):
                 sql_query_generada = tool_calls[0].tool_input.get('query', "")
        
        # --- Ejecutamos la validación del "Guardia de Seguridad" ---
        if sql_query_generada and not validar_sql(sql_query_generada, empresa_id_from_prompt):
             # Si la validación falla, ignoramos la respuesta de la IA y devolvemos un error de acceso denegado.
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