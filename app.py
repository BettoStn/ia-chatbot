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

# --- **FINAL AND DEFINITIVE SECURITY GUARD** ---
def is_sql_safe(sql_query: str, user_empresa_id: int):
    """
    This function performs a strict, multi-layered security check on the AI-generated SQL.
    Returns True if safe, False otherwise.
    """
    lower_sql = sql_query.lower().strip()
    
    # Layer 1: Deny any command that is not a SELECT statement.
    if not lower_sql.startswith('select'):
        print(f"SECURITY ALERT: Blocked non-SELECT statement. User Empresa ID: {user_empresa_id}, Query: {sql_query}")
        return False

    # Layer 2: Deny any command containing dangerous keywords.
    forbidden_keywords = ['update', 'delete', 'insert', 'drop', 'alter', 'truncate', 'grant', 'revoke', 'into', 'from users']
    if any(keyword in lower_sql for keyword in forbidden_keywords):
        print(f"SECURITY ALERT: Blocked forbidden keyword. User Empresa ID: {user_empresa_id}, Query: {sql_query}")
        return False
        
    # Layer 3: Enforce multi-tenancy. Check for attempts to access other companies.
    # This regex finds all instances of 'empresa_id = [number]'.
    empresa_id_references = re.findall(r'empresa_id\s*=\s*(\d+)', lower_sql)
    for found_id in empresa_id_references:
        if int(found_id) != user_empresa_id:
            print(f"SECURITY ALERT: Blocked attempt to access forbidden empresa_id={found_id}. User is {user_empresa_id}. Query: {sql_query}")
            return False
            
    # Case for querying the 'empresas' table itself. It must only be for the user's own company.
    if 'from empresas' in lower_sql:
        # This regex finds all instances of 'id = [number]' or 'empresas.id = [number]'.
        id_references = re.findall(r"(?:empresas\.)?id\s*=\s*(\d+)", lower_sql)
        for found_id in id_references:
            if int(found_id) != user_empresa_id:
                print(f"SECURITY ALERT: Blocked attempt to access forbidden empresas.id={found_id}. User is {user_empresa_id}. Query: {sql_query}")
                return False

    return True # If all checks pass, the query is deemed safe.

@app.route('/', methods=['POST', 'OPTIONS'])
def handle_query():
    if request.method == 'OPTIONS':
        return '', 204
    try:
        body = request.get_json()
        prompt_completo = body.get('pregunta', '')
        
        if not prompt_completo:
            return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

        # Extract the user's company ID from the prompt for the security check.
        empresa_id_match = re.search(r'empresa_id = (\d+)', prompt_completo)
        if not empresa_id_match:
            return jsonify({"error": "Error de seguridad: ID de empresa no encontrado en la solicitud."}), 400
        user_empresa_id = int(empresa_id_match.group(1))

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
        
        # --- **SECURITY CHECKPOINT** ---
        # The AI's generated SQL is intercepted and validated here.
        if sql_query_generada and not is_sql_safe(sql_query_generada, user_empresa_id):
             # If validation fails, we override the AI's response with a denial message.
             respuesta_final = "Lo siento, la consulta solicitada no está permitida por razones de seguridad."
        else:
             # If validation passes, we use the AI's normal response.
             respuesta_final = resultado_agente.get("output", "No se pudo obtener una respuesta.")

        return jsonify({"respuesta": respuesta_final})

    except Exception as e:
        print(f"Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))