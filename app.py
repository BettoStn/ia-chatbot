# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
import base64
from langchain_deepseek import ChatDeepSeek
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

app = Flask(__name__)
CORS(app)

# --- **GUARDIA DE SEGURIDAD FINAL Y DEFINITIVO** ---
def is_sql_safe(sql_query: str, user_empresa_id: int):
    """
    Valida el SQL generado por la IA con reglas de seguridad estrictas e inquebrantables.
    Devuelve True si es seguro, False si no lo es.
    """
    lower_sql = sql_query.lower().strip()
    
    # Regla 1: Solo permitir consultas SELECT.
    if not lower_sql.startswith('select'):
        print(f"SECURITY ALERT (NON-SELECT): User Empresa ID: {user_empresa_id}, Query: {sql_query}")
        return False

    # Regla 2: Prohibir palabras clave peligrosas.
    forbidden_keywords = ['update', 'delete', 'insert', 'drop', 'alter', 'truncate', 'grant', 'revoke']
    if any(keyword in lower_sql for keyword in forbidden_keywords):
        print(f"SECURITY ALERT (FORBIDDEN KEYWORD): User Empresa ID: {user_empresa_id}, Query: {sql_query}")
        return False
        
    # --- LÓGICA DE SEGURIDAD MULTI-EMPRESA REFORZADA ---

    # CASO ESPECIAL: La consulta es sobre la tabla 'empresas'
    if 'from empresas' in lower_sql:
        id_filter_pattern = re.compile(r"(?:empresas\.)?id\s*=\s*" + str(user_empresa_id))
        if not id_filter_pattern.search(lower_sql):
            print(f"SECURITY ALERT (BROAD QUERY ON 'empresas'): User Empresa ID: {user_empresa_id}, Query: {sql_query}")
            return False
    
    # CASO GENERAL: La consulta es sobre cualquier otra tabla de negocio
    elif 'empresa_id' in lower_sql:
        empresa_filter_pattern = re.compile(r"empresa_id\s*=\s*" + str(user_empresa_id))
        if not empresa_filter_pattern.search(lower_sql):
            print(f"SECURITY ALERT (MISSING/WRONG empresa_id): User Empresa ID: {user_empresa_id}, Query: {sql_query}")
            return False

    # Regla final: Verificamos que no se intente colar el ID de otra empresa en NINGÚN CASO
    all_empresa_ids = re.findall(r'empresa_id\s*=\s*(\d+)', lower_sql)
    for eid in all_empresa_ids:
        if int(eid) != user_empresa_id:
            print(f"SECURITY ALERT (FORBIDDEN empresa_id={eid}): User is {user_empresa_id}, Query: {sql_query}")
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
        user_empresa_id = int(empresa_id_match.group(1))

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        db_uri = os.environ.get("DATABASE_URI")
        llm = ChatDeepSeek(model="deepseek-chat", api_key=api_key, temperature=0)
        
        # 📌 Mantenemos la configuración de SQLDatabase como estaba
        db = SQLDatabase.from_uri(db_uri)
        
        # 📌 El agente limitará los resultados a 20 por defecto para el chat
        agent_executor = create_sql_agent(llm, db=db, agent_type="openai-tools", verbose=True, max_rows_to_display=20)

        resultado_agente = agent_executor.invoke({"input": prompt_completo})
        
        intermediate_steps = resultado_agente.get("intermediate_steps", [])
        sql_query_generada = ""
        if intermediate_steps:
            tool_calls = intermediate_steps[0]
            if tool_calls and hasattr(tool_calls[0], 'tool_input') and isinstance(tool_calls[0].tool_input, dict):
                 sql_query_generada = tool_calls[0].tool_input.get('query', "")
        
        # --- **PUNTO DE CONTROL FINAL (SEGURIDAD + REPORTES)** ---
        if sql_query_generada:
            # 1. Chequeo de Seguridad
            if not is_sql_safe(sql_query_generada, user_empresa_id):
                respuesta_final = "Lo siento, la consulta solicitada no está permitida por razones de seguridad."
            else:
                # 2. Chequeo de Tamaño de Resultados (usando la consulta SIN LIMIT)
                # Quitamos la cláusula LIMIT que el agente pudo haber agregado
                full_sql_query = re.sub(r'\s+LIMIT\s+\d+\s*$', '', sql_query_generada, flags=re.IGNORECASE)
                
                # Manejamos los resultados para evitar errores si no se encuentra un número
                try:
                    count_result = db.run(f"SELECT COUNT(*) FROM ({full_sql_query}) as subquery")
                    record_count = int("".join(filter(str.isdigit, count_result)))
                except (ValueError, TypeError):
                    record_count = 0

                if record_count == 0:
                    respuesta_final = "No se encontraron resultados para esta consulta."
                elif record_count > 20:
                    encoded_query = base64.b64encode(full_sql_query.encode('utf-8')).decode('utf-8')
                    download_url = f"https://bodezy.com/vistas/exportar-reporte.php?query={encoded_query}" 
                    respuesta_final = (f"He encontrado **{record_count} registros**, lo cual es mucho para mostrar en el chat.\n\n"
                                     f"He preparado un reporte para que lo descargues directamente:\n\n"
                                     f"📥 [**Descargar Reporte Completo en Excel**]({download_url})")
                else:
                    respuesta_final = resultado_agente.get("output", "No se pudo obtener una respuesta.")
        else:
            respuesta_final = resultado_agente.get("output", "No se pudo obtener una respuesta.")

        return jsonify({"respuesta": respuesta_final})

    except Exception as e:
        print(f"Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))