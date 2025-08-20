from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
from langchain_openai import ChatOpenAI
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

app = Flask(__name__)
CORS(app)

# --- GUARDIA DE SEGURIDAD ---
def is_sql_safe(sql_query: str, user_empresa_id: int):
    lower_sql = sql_query.lower().strip()
    if not lower_sql.startswith("select"):
        print(f"SECURITY ALERT (NON-SELECT): User {user_empresa_id}, Query: {sql_query}")
        return False

    forbidden_keywords = ["update", "delete", "insert", "drop", "alter", "truncate", "grant", "revoke"]
    if any(keyword in lower_sql for keyword in forbidden_keywords):
        print(f"SECURITY ALERT (FORBIDDEN KEYWORD): User {user_empresa_id}, Query: {sql_query}")
        return False

    if "from empresas" in lower_sql:
        id_filter_pattern = re.compile(r"(?:empresas\.)?id\s*=\s*" + str(user_empresa_id))
        if not id_filter_pattern.search(lower_sql):
            print(f"SECURITY ALERT (BROAD QUERY ON 'empresas'): User {user_empresa_id}, Query: {sql_query}")
            return False
    elif "empresa_id" in lower_sql:
        empresa_filter_pattern = re.compile(r"empresa_id\s*=\s*" + str(user_empresa_id))
        if not empresa_filter_pattern.search(lower_sql):
            print(f"SECURITY ALERT (MISSING/WRONG empresa_id): User {user_empresa_id}, Query: {sql_query}")
            return False

    all_empresa_ids = re.findall(r"empresa_id\s*=\s*(\d+)", lower_sql)
    for eid in all_empresa_ids:
        if int(eid) != user_empresa_id:
            print(f"SECURITY ALERT (FORBIDDEN empresa_id={eid}): User {user_empresa_id}, Query: {sql_query}")
            return False

    return True


# --- Extractor robusto de SQL desde el resultado del agente ---
def extract_sql_from_agent_result(result) -> str:
    """
    Intenta recuperar el SQL de:
    - intermediate_steps: action.tool_input (dict o str), action.log (```sql ... ```), observation
    - output final: bloque ```sql ... ``` o primer SELECT plausible
    """
    def first_select(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"```sql\s*(.*?)\s*```", text, flags=re.S | re.I)
        if m:
            cand = m.group(1).strip()
            if cand.lower().startswith("select"):
                return cand
        m2 = re.search(r"(SELECT\s+[\s\S]+)", text, flags=re.I)
        if m2:
            cand = m2.group(1).strip()
            cand = re.split(r"\n\s*```", cand)[0]
            if cand.lower().startswith("select"):
                return cand
        return ""

    # 1) intermediate_steps
    try:
        steps = result.get("intermediate_steps", []) or []
        for step in steps:
            # step puede ser (AgentAction, observation_str) o dicts; seamos defensivos
            action = None
            observation = ""
            if isinstance(step, (list, tuple)):
                action = step[0] if len(step) > 0 else None
                observation = step[1] if len(step) > 1 else ""
            elif isinstance(step, dict):
                action = step.get("action")
                observation = step.get("observation", "")

            # a) tool_input
            if action is not None:
                tool_input = getattr(action, "tool_input", None)
                if isinstance(tool_input, dict):
                    for k in ("query", "input"):
                        v = tool_input.get(k)
                        if isinstance(v, str) and v.strip().lower().startswith("select"):
                            return v
                if isinstance(tool_input, str) and tool_input.strip().lower().startswith("select"):
                    return tool_input

                # b) action.log
                action_log = getattr(action, "log", "") or ""
                cand = first_select(action_log)
                if cand:
                    return cand

            # c) observation (a veces trae el bloque del SQL que se ejecutó)
            if isinstance(observation, str):
                cand = first_select(observation)
                if cand:
                    return cand
    except Exception:
        pass

    # 2) output final
    try:
        out = result.get("output", "") or ""
        cand = first_select(out)
        if cand:
            return cand
    except Exception:
        pass

    return ""


def force_sql_only(llm: ChatOpenAI, prompt_usuario: str, empresa_id: int) -> str:
    """
    Fallback: cuando el extractor falla pero el usuario pidió explícitamente el SQL,
    pedimos al LLM que devuelva SOLO una consulta SELECT válida y filtrada por empresa_id.
    """
    system = (
        "Eres un asistente que SOLO devuelve una consulta SQL válida que comience con SELECT.\n"
        "No devuelvas ningún texto explicativo, ni comentarios, ni bloques de markdown; únicamente la sentencia SQL.\n"
        f"La consulta debe incluir un filtro exacto: empresa_id = {empresa_id}.\n"
    )
    # Pedimos reescribir la intención del usuario como SQL SELECT
    content = (
        f"{system}\n\n"
        f"Intención del usuario: {prompt_usuario}\n"
        "Devuelve SOLO la consulta (una línea o múltiples líneas) pero únicamente la sentencia SQL."
    )
    resp = llm.invoke(content)
    sql = resp.content.strip()
    # Limpiar si el modelo devolvió por error un bloque
    sql = re.sub(r"^```sql\s*|\s*```$", "", sql, flags=re.I).strip()
    return sql


@app.route("/", methods=["POST", "OPTIONS"])
def handle_query():
    if request.method == "OPTIONS":
        return "", 204
    try:
        body = request.get_json() or {}
        prompt_completo = body.get("pregunta", "")

        # --- Ejecución directa de SQL ---
        sql_query_directa = body.get("sql_query")
        if sql_query_directa:
            user_empresa_id = body.get("empresa_id")
            if not user_empresa_id:
                return jsonify({"error": "Error de seguridad: No se pudo determinar el ID de la empresa."}), 400

            db_uri = os.environ.get("DATABASE_URI")
            db = SQLDatabase.from_uri(db_uri)

            if not is_sql_safe(sql_query_directa, user_empresa_id):
                return jsonify({"error": "La consulta solicitada no está permitida por razones de seguridad."}), 403

            try:
                results = db.run(sql_query_directa)
                return jsonify({"results": results, "message": "Consulta ejecutada exitosamente."})
            except Exception as e:
                return jsonify({"error": f"Error al ejecutar la consulta: {str(e)}"}), 500

        # --- Flujo NL ---
        if not prompt_completo:
            return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

        empresa_id_match = re.search(r"empresa_id\s*=\s*(\d+)", prompt_completo)
        if not empresa_id_match:
            return jsonify({"error": "Error de seguridad: No se pudo determinar el ID de la empresa."}), 400
        user_empresa_id = int(empresa_id_match.group(1))

        api_key = os.environ.get("OPENAI_API_KEY")
        db_uri = os.environ.get("DATABASE_URI")

        llm = ChatOpenAI(
            model_name="gpt-4o-mini",
            temperature=0,
            openai_api_key=api_key,
        )
        db = SQLDatabase.from_uri(db_uri)

        agent_executor = create_sql_agent(
            llm,
            db=db,
            agent_type="openai-tools",
            verbose=True,
            agent_executor_kwargs={"top_k": 99999},
        )

        resultado_agente = agent_executor.invoke({"input": prompt_completo})

        # --- Extraer SQL de forma robusta ---
        sql_query_generada = extract_sql_from_agent_result(resultado_agente)

        # Palabras que indican que QUIERES el SQL literal
        sql_intent_keywords = [
            "sql", "consulta sql", "dame el sql", "dame la consulta",
            "query", "código sql", "codigo sql", "genera el sql",
            "exportar", "exportación", "todos", "completo", "listado", "descargar", "reporte de"
        ]
        lower_prompt = prompt_completo.lower()
        quiere_sql = any(kw in lower_prompt for kw in sql_intent_keywords)

        # Si falló el extractor pero el usuario pidió explícitamente el SQL, forzamos generación
        if quiere_sql and not sql_query_generada:
            sql_query_generada = force_sql_only(llm, prompt_completo, user_empresa_id)

        # --- SEGURIDAD + RESPUESTA ---
        if sql_query_generada:
            if not is_sql_safe(sql_query_generada, user_empresa_id):
                return jsonify({"respuesta": "Lo siento, la consulta solicitada no está permitida por razones de seguridad."})

            # quitar LIMIT final para exportar
            full_sql_query = re.sub(r"\s+LIMIT\s+\d+\s*$", "", sql_query_generada, flags=re.IGNORECASE)

            if quiere_sql:
                # Mostrar SOLO el bloque SQL
                return jsonify({
                    "sql_code": full_sql_query,
                    "message": f"```sql\n{full_sql_query}\n```"
                })

            # Si no pidió el SQL, decidir por tamaño
            try:
                count_result = db.run(f"SELECT COUNT(*) FROM ({full_sql_query}) AS subquery")
                record_count = int("".join(filter(str.isdigit, str(count_result))))
            except Exception:
                record_count = 0

            if record_count == 0:
                return jsonify({"respuesta": "No se encontraron resultados para esta consulta."})
            elif record_count > 10:
                # También SOLO el bloque SQL (sin textos extra)
                return jsonify({
                    "sql_code": full_sql_query,
                    "message": f"```sql\n{full_sql_query}\n```"
                })
            else:
                return jsonify({"respuesta": resultado_agente.get("output", "No se pudo obtener una respuesta.")})
        else:
            # Sin SQL extraíble ni intención explícita -> salida del agente
            return jsonify({"respuesta": resultado_agente.get("output", "No se pudo obtener una respuesta.")})

    except Exception as e:
        print(f"Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
