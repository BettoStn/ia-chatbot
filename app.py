# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
from langchain_deepseek import ChatDeepSeek
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

app = Flask(__name__)
CORS(app)

# --- **GUARDIA DE SEGURIDAD FINAL Y CORREGIDO** ---
def validar_sql(sql_query: str, empresa_id: int):
    """
    Valida el SQL generado por la IA con reglas de seguridad estrictas.
    Devuelve True si es seguro, False si no lo es.
    """
    lower_sql = sql_query.lower().strip()

    # Regla 1: Solo permitir consultas SELECT.
    if not lower_sql.startswith('select'):
        print(f"VALIDATION FAILED: Not a SELECT statement.")
        return False

    # Regla 2: Prohibir palabras clave peligrosas.
    forbidden_keywords = ['update', 'delete', 'insert', 'drop', 'alter', 'truncate', 'grant', 'revoke']
    if any(keyword in lower_sql for keyword in forbidden_keywords):
        print(f"VALIDATION FAILED: Contains forbidden keywords.")
        return False
        
    # --- LÓGICA DE SEGURIDAD MEJORADA Y CORREGIDA ---

    # CASO ESPECIAL: La consulta es sobre la tabla 'empresas'
    if 'from empresas' in lower_sql:
        # La consulta DEBE buscar por el 'id' de la empresa del usuario y NINGÚN OTRO.
        # Buscamos patrones como "id = 9" o "empresas.id = 9"
        id_filter_pattern = re.compile(r"(?:empresas\.)?id\s*=\s*(\d+)")
        matches = id_filter_pattern.findall(lower_sql)
        
        # Debe haber exactamente un filtro por id
        if len(matches) != 1:
            print(f"VALIDATION FAILED: Invalid or missing ID filter on 'empresas' table.")
            return False
        
        found_id = int(matches[0])

        # El ID encontrado debe ser el del usuario que ha iniciado sesión
        if found_id != empresa_id:
            print(f"VALIDATION FAILED: Attempted to access forbidden empresa.id={found_id}.")
            return False

    # CASO GENERAL: La consulta es sobre cualquier otra tabla de negocio
    else:
        # La consulta DEBE contener el filtro del 'empresa_id' del usuario.
        empresa_filter_pattern = re.compile(r"empresa_id\s*=\s*" + str(empresa_id))
        if not empresa_filter_pattern.search(lower_sql):
            print(f"VALIDATION FAILED: Missing correct empresa_id filter.")
            return False

        # Adicionalmente, verificamos que NO se intente filtrar por OTRO empresa_id.
        all_empresa_ids = re.findall(r'empresa_id\s*=\s*(\d+)', lower_sql)
        for eid in all_empresa_ids:
            if int(eid) != empresa_id:
                print(f"VALIDATION FAILED: Attempted to access forbidden empresa_id={eid}.")
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

        empresa_id_match = re.search(r'empresa_id = (\d+)', prompt_completo)
        if not empresa_id_match:
            return jsonify({"error": "Error de seguridad: No se pudo determinar el ID de la empresa."}), 400
        empresa_id_from_prompt = int(empresa_id_match.group(1))

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        db_uri = os.environ.get("DATABASE_URI")
        llm = ChatDeepSeek(model="deepseek-chat", api_key=api_key, temperature=0)
        db = SQLDatabase.from_uri(db_uri)
        
        agent_executor = create_sql_agent(llm, db=db, agent_type="openai-tools", verbose=True)
        resultado_agente = agent_executor.invoke({"input": prompt_completo})
        
        intermediate_steps = resultado_agente.get("intermediate_steps", [])
        sql_query_generada = ""
        if intermediate_steps:
            tool_calls = intermediate_steps[0]
            if tool_calls and hasattr(tool_calls[0], 'tool_input') and isinstance(tool_calls[0].tool_input, dict):
                 sql_query_generada = tool_calls[0].tool_input.get('query', "")
        
        # --- Punto de Control del "Guardia de Seguridad" ---
        if sql_query_generada and not validar_sql(sql_query_generada, empresa_id_from_prompt):
             respuesta_final = "Lo siento, no tengo permiso para realizar esa consulta."
        else:
             respuesta_final = resultado_agente.get("output", "No se pudo obtener una respuesta.")

        return jsonify({"respuesta": respuesta_final})

    except Exception as e:
        print(f"Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))