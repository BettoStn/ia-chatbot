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
    sql = ""

    # 1) Intentar desde intermediate_steps
    try:
        steps = result.get("intermediate_steps", [])
        for step in steps:
            # step suele ser (AgentAction, str_observation)
            action = step[0] if isinstance(step, (list, tuple)) and step else None
            if action is None:
                continue

            tool_input = getattr(action, "tool_input", None)
            # a) tool_input dict con 'query' o 'input'
            if isinstance(tool_input, dict):
                for k in ("query", "input"):
                    v = tool_input.get(k)
                    if isinstance(v, str) and v.strip().lower().startswith("select"):
                        return v

            # b) tool_input como cadena
            if isinstance(tool_input, str) and tool_input.strip().lower().startswith("select"):
                return tool_input

            # c) bloque ```sql en el log
            action_log = getattr(action, "log", "") or ""
            m = re.search(r"```sql\s*(.*?)\s*```", action_log, flags=re.S | re.I)
            if m:
                candidate = m.group(1).strip()
                if candidate.lower().startswith("select"):
                    return candidate

            # d) primer SELECT plausible en el log
            m2 = re.search(r"(SELECT\s+[\s\S]+)", action_log, flags=re.I)
            if m2:
                candidate = m2.group(1).strip()
                candidate = re.split(r"\n\s*```", candidate)[0]
                if candidate.lower().startswith("select"):
                    return candidate
    except Exception:
        pass

    # 2) Intentar desde output final (bloque ```sql)
    try:
        out = result.get("output", "") or ""
        m = re.search(r"```sql\s*(.*?)\s*```", out, flags=re.S | re.I)
        if m:
            candidate = m.group(1).strip()
            if candidate.lower().startswith("select"):
                return candidate
    except Exception:
        pass

    # 3) fallback: primer SELECT en output
    try:
        out = result.get("output", "") or ""
        m = re.search(r"(SELECT\s+[\s\S]+)", out, flags=re.I)
        if m:
            candidate = m.group(1).strip()
            candidate = re.split(r"\n\s*```", candidate)[0]
            if candidate.lower().startswith("select"):
                return candidate
    except Exception:
        pass

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

        # --- SEGURIDAD + LÓGICA DE RESPUESTA ---
        if sql_query_generada:
            if not is_sql_safe(sql_query_generada, user_empresa_id):
                respuesta_final = {"respuesta": "Lo siento, la consulta solicitada no está permitida por razones de seguridad."}
            else:
                # Quitar LIMIT final para exportar
                full_sql_query = re.sub(r"\s+LIMIT\s+\d+\s*$", "", sql_query_generada, flags=re.IGNORECASE)

                # Intención explícita de ver el SQL literal
                sql_intent_keywords = [
                    "sql", "consulta sql", "dame el sql", "dame la consulta",
                    "query", "código sql", "codigo sql", "genera el sql",
                    "exportar", "exportación", "todos", "completo", "listado", "descargar", "reporte de"
                ]
                lower_prompt = prompt_completo.lower()
                quiere_sql = any(kw in lower_prompt for kw in sql_intent_keywords)

                if quiere_sql:
                    # Mostrar SOLO el bloque SQL
                    respuesta_final = {
                        "sql_code": full_sql_query,
                        "message": f"```sql\n{full_sql_query}\n```"
                    }
                else:
                    # (Opcional) contar resultados para decidir mostrar SQL si es grande
                    try:
                        count_result = db.run(f"SELECT COUNT(*) FROM ({full_sql_query}) AS subquery")
                        record_count = int("".join(filter(str.isdigit, str(count_result))))
                    except Exception:
                        record_count = 0

                    if record_count == 0:
                        respuesta_final = {"respuesta": "No se encontraron resultados para esta consulta."}
                    elif record_count > 10:
                        # También SOLO el bloque SQL (sin textos extra)
                        respuesta_final = {
                            "sql_code": full_sql_query,
                            "message": f"```sql\n{full_sql_query}\n```"
                        }
                    else:
                        # Respuesta conversacional normal del agente
                        respuesta_final = {"respuesta": resultado_agente.get("output", "No se pudo obtener una respuesta.")}
        else:
            # Si no logramos extraer SQL, devolvemos el output del agente
            respuesta_final = {"respuesta": resultado_agente.get("output", "No se pudo obtener una respuesta.")}

        return jsonify(respuesta_final)

    except Exception as e:
        print(f"Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
