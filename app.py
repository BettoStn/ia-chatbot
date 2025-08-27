from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
from langchain_openai import ChatOpenAI
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent
import sys

app = Flask(__name__)
CORS(app)

# --- VERIFICACIÓN INICIAL DE VARIABLES DE ENTORNO ---
# Si falta alguna variable crítica, el servidor no iniciará y el log mostrará el error.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
DATABASE_URI = os.environ.get("DATABASE_URI")

if not OPENAI_API_KEY:
    print("ERROR CRÍTICO: La variable de entorno OPENAI_API_KEY no está configurada.")
    sys.exit(1)
if not DATABASE_URI:
    print("ERROR CRÍTICO: La variable de entorno DATABASE_URI no está configurada.")
    sys.exit(1)

# --- GUARDIA DE SEGURIDAD (Sin cambios, ya estaba bien) ---
def is_sql_safe(sql_query: str, user_empresa_id: int):
    # (Tu código de seguridad aquí... se mantiene igual)
    lower_sql = sql_query.lower().strip()
    if not lower_sql.startswith("select"):
        print(f"SECURITY ALERT (NON-SELECT): User {user_empresa_id}, Query: {sql_query}")
        return False
    forbidden_keywords = ["update", "delete", "insert", "drop", "alter", "truncate", "grant", "revoke"]
    if any(keyword in lower_sql for keyword in forbidden_keywords):
        print(f"SECURITY ALERT (FORBIDDEN KEYWORD): User {user_empresa_id}, Query: {sql_query}")
        return False
    empresa_filter_pattern = re.compile(r"empresa_id\s*=\s*" + str(user_empresa_id))
    if "empresa_id" in lower_sql and not empresa_filter_pattern.search(lower_sql):
        print(f"SECURITY ALERT (MISSING/WRONG empresa_id): User {user_empresa_id}, Query: {sql_query}")
        return False
    all_empresa_ids_in_query = re.findall(r"empresa_id\s*=\s*(\d+)", lower_sql)
    for eid in all_empresa_ids_in_query:
        if int(eid) != user_empresa_id:
            print(f"SECURITY ALERT (FORBIDDEN empresa_id={eid}): User {user_empresa_id}, Query: {sql_query}")
            return False
    return True

# --- EXTRACTOR DE SQL (Sin cambios) ---
def extract_sql_from_agent_result(result) -> str:
    # (Tu código de extracción aquí... se mantiene igual)
    # Es una función muy robusta, está bien diseñada.
    def first_select(text: str) -> str:
        if not text: return ""
        m = re.search(r"```sql\s*(.*?)\s*```", text, flags=re.S | re.I)
        if m:
            cand = m.group(1).strip()
            if cand.lower().startswith("select"): return cand
        m2 = re.search(r"(SELECT\s+[\s\S]+)", text, flags=re.I)
        if m2:
            cand = m2.group(1).strip()
            cand = re.split(r"\n\s*```", cand)[0]
            if cand.lower().startswith("select"): return cand
        return ""
    try:
        steps = result.get("intermediate_steps", []) or []
        for step in steps:
            action, observation = None, ""
            if isinstance(step, (list, tuple)):
                action = step[0] if len(step) > 0 else None
                observation = step[1] if len(step) > 1 else ""
            if action:
                tool_input = getattr(action, "tool_input", None)
                if isinstance(tool_input, dict):
                    for k in ("query", "input"):
                        v = tool_input.get(k)
                        if isinstance(v, str) and v.strip().lower().startswith("select"): return v
                if isinstance(tool_input, str) and tool_input.strip().lower().startswith("select"): return tool_input
                action_log = getattr(action, "log", "") or ""
                cand = first_select(action_log)
                if cand: return cand
    except Exception: pass
    try:
        out = result.get("output", "") or ""
        cand = first_select(out)
        if cand: return cand
    except Exception: pass
    return ""

@app.route("/", methods=["POST", "OPTIONS"])
def handle_query():
    if request.method == "OPTIONS":
        return "", 204
    try:
        body = request.get_json() or {}
        prompt_completo = body.get("pregunta", "")

        if not prompt_completo:
            return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

        print(f"Recibida pregunta: {prompt_completo[:100]}...")

        empresa_id_match = re.search(r"empresa_id\s*=\s*(\d+)", prompt_completo)
        if not empresa_id_match:
            return jsonify({"error": "Error de seguridad: No se pudo determinar el ID de la empresa en el prompt."}), 400
        user_empresa_id = int(empresa_id_match.group(1))
        
        print("Paso 1: Inicializando LLM...")
        llm = ChatOpenAI(
            # MEJORA: Usar gpt-5-nano para mejor calidad en SQL.
            model_name="gpt-5-nano", 
            temperature=0,
            openai_api_key=OPENAI_API_KEY,
        )
        
        print("Paso 2: Conectando a la base de datos...")
        db = SQLDatabase.from_uri(DATABASE_URI)

        print("Paso 3: Creando el agente SQL...")
        agent_executor = create_sql_agent(
            llm,
            db=db,
            agent_type="openai-tools",
            verbose=True,
        )

        print("Paso 4: Invocando el agente (puede tardar)...")
        resultado_agente = agent_executor.invoke({"input": prompt_completo})
        print("Paso 5: El agente finalizó la ejecución.")

        # --- Extracción y Lógica de Respuesta (Sin cambios, ya estaba bien) ---
        sql_query_generada = extract_sql_from_agent_result(resultado_agente)
        
        if sql_query_generada:
            print(f"SQL extraído: {sql_query_generada[:100]}...")
            if not is_sql_safe(sql_query_generada, user_empresa_id):
                return jsonify({"respuesta": "Lo siento, la consulta generada no está permitida por razones de seguridad."})
            
            # Si el usuario quiere exportar, devolvemos el SQL completo
            sql_intent_keywords = ["exportar", "descargar", "reporte de", "todos mis clientes", "ventas de este mes"]
            if any(kw in prompt_completo.lower() for kw in sql_intent_keywords):
                print("Intención de exportación detectada. Devolviendo SQL.")
                return jsonify({
                    "sql_code": sql_query_generada,
                    "message": f"Entendido. He preparado la consulta para que la exportes. Puedes ejecutarla desde el panel de la derecha.\n\n```sql\n{sql_query_generada}\n```"
                })
            
            # Si no, devolvemos la respuesta del agente
            print("Devolviendo respuesta conversacional del agente.")
            return jsonify({"respuesta": resultado_agente.get("output", "No se pudo obtener una respuesta clara.")})
        else:
            print("No se pudo extraer SQL. Devolviendo respuesta directa del agente.")
            return jsonify({"respuesta": resultado_agente.get("output", "No pude generar una consulta SQL para tu pregunta.")})

    except Exception as e:
        # MEJORA: Imprime el error completo en el log de Render para una mejor depuración
        print(f"!!! ERROR INESPERADO EN EL SERVIDOR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Ocurrió un error inesperado en el servidor al procesar la solicitud."}), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
