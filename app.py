from flask import Flask, request, jsonify
from flask_cors import CORS
import os, re, json, sys, traceback

from langchain_openai import ChatOpenAI
from langchain_community.utilities import SQLDatabase
from langchain_core.messages import SystemMessage, HumanMessage

app = Flask(__name__)
CORS(app)

# =========================
#   CONFIG / ENTORNO
# =========================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
DATABASE_URI   = os.environ.get("DATABASE_URI")

if not OPENAI_API_KEY:
    print("ERROR CRÍTICO: La variable de entorno OPENAI_API_KEY no está configurada.")
    sys.exit(1)
if not DATABASE_URI:
    print("ERROR CRÍTICO: La variable de entorno DATABASE_URI no está configurada.")
    sys.exit(1)

MAX_PREVIEW_ROWS = 1000
EXPORT_KEYWORDS = [
    "exportar", "descargar", "csv", "excel", "xlsx", "reporte de", "reporte",
    "exportación", "export", "todos mis clientes", "ventas de este mes",
    "todas las ventas", "listado completo", "dump"
]

# =========================
#   GUARDIAS DE SEGURIDAD
# =========================
def is_sql_safe(sql_query: str, user_empresa_id: int) -> bool:
    q = (sql_query or "").strip()
    lower_sql = q.lower()
    # Debe ser SELECT (o WITH ... SELECT)
    if not re.match(r'^\s*(with\s+.*?select|select)\b', lower_sql, flags=re.S|re.I):
        print(f"SECURITY ALERT (NON-SELECT): User {user_empresa_id}, Query: {q}")
        return False
    # Bloquear DDL/DML peligrosos
    if re.search(r'\b(insert|update|delete|merge|alter|drop|truncate|create|grant|revoke)\b', lower_sql):
        print(f"SECURITY ALERT (FORBIDDEN KEYWORD): User {user_empresa_id}, Query: {q}")
        return False
    # Si aparece empresa_id, debe ser el mismo
    if "empresa_id" in lower_sql:
        all_ids = re.findall(r'empresa_id\s*=\s*(\d+)', lower_sql)
        if any(int(eid) != user_empresa_id for eid in all_ids):
            print(f"SECURITY ALERT (FORBIDDEN empresa_id in query): User {user_empresa_id}, Query: {q}")
            return False
    return True

def ensure_empresa_filter(sql: str, empresa_id: int) -> str:
    """
    Inyecta un filtro por empresa_id si no existe explícitamente.
    - Si ya hay WHERE, agrega AND.
    - Si no hay WHERE, crea uno antes de ORDER/GROUP/LIMIT o al final.
    """
    if "empresa_id" in sql.lower():
        return sql

    # Punto de inserción antes de ORDER/GROUP/LIMIT (si existen)
    m = re.search(r'(?i)\b(order\s+by|group\s+by|limit)\b', sql)
    clause = f" empresa_id = {empresa_id} "

    if re.search(r'(?i)\bwhere\b', sql):
        if m:
            idx = m.start()
            return sql[:idx] + f" AND {clause}" + sql[idx:]
        return sql.rstrip() + f" AND {clause}"
    else:
        if m:
            idx = m.start()
            return sql[:idx] + f" WHERE {clause}" + sql[idx:]
        return sql.rstrip().rstrip(';') + f" WHERE {clause};"

def add_limit(sql: str, n: int) -> str:
    if re.search(r'(?i)\blimit\b', sql):
        return sql
    sql = sql.rstrip().rstrip(';')
    return f"{sql} LIMIT {n};"

def looks_like_export_intent(prompt: str) -> bool:
    p = (prompt or "").lower()
    return any(kw in p for kw in EXPORT_KEYWORDS)

# =========================
#   PROMPTS
# =========================
SYSTEM_PROMPT = """Eres un generador de SQL para MariaDB.
REGLAS:
- Responde SOLO UN JSON con esta forma exacta: {"sql":"...", "mode":"preview|export", "notes":"..."}.
- SOLO SELECT (o WITH ... SELECT). Prohibido DDL/DML: INSERT, UPDATE, DELETE, ALTER, DROP, TRUNCATE, CREATE, GRANT, REVOKE.
- NUNCA devuelvas filas, resultados, tablas, CSV ni datos. SOLO genera el SQL.
- Si el usuario quiere exportar/descargar/reporte masivo, usa "mode":"export" y NO añadas LIMIT.
- En caso contrario usa "mode":"preview" y añade LIMIT {limit}.
- Siempre filtra por empresa_id = {empresa_id} donde aplique.
- Dialecto: MariaDB. Usa nombres de tablas y columnas tal cual.
"""

USER_TEMPLATE = """Esquema (resumen):
{schema}

Tarea del usuario:
{pregunta}

Recuerda: responde SOLO un JSON válido con llaves {{}}, sin texto extra afuera.
"""

# =========================
#   ENDPOINT
# =========================
@app.route("/", methods=["POST", "OPTIONS"])
def handle_query():
    if request.method == "OPTIONS":
        return "", 204
    try:
        body = request.get_json() or {}
        prompt_completo = (body.get("pregunta") or "").strip()

        if not prompt_completo:
            return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

        print(f"[INFO] Pregunta: {prompt_completo[:160]}...")

        empresa_id_match = re.search(r"empresa_id\s*=\s*(\d+)", prompt_completo, flags=re.I)
        if not empresa_id_match:
            return jsonify({"error": "Error de seguridad: No se pudo determinar el ID de la empresa en el prompt (empresa_id=...)."}), 400
        user_empresa_id = int(empresa_id_match.group(1))

        # Modelo por defecto: gpt-5-mini (mejor balance costo/inteligencia para SQL)
        llm = ChatOpenAI(
            model_name="gpt-5-mini",
            temperature=0,
            openai_api_key=OPENAI_API_KEY,
            max_tokens=350  # evita salidas largas
        )

        # Solo para dar contexto mínimo del esquema (no ejecuta SELECT masivos)
        db = SQLDatabase.from_uri(DATABASE_URI)
        schema_text = db.get_table_info()
        # Opcional: recortar esquema si es muy grande para reducir costo
        if len(schema_text) > 12000:
            schema_text = schema_text[:12000] + "\n-- [Schema truncado para brevedad]"

        # Construir mensajes
        sys_msg = SystemMessage(content=SYSTEM_PROMPT.format(
            limit=MAX_PREVIEW_ROWS,
            empresa_id=user_empresa_id
        ))
        user_msg = HumanMessage(content=USER_TEMPLATE.format(
            schema=schema_text,
            pregunta=prompt_completo
        ))

        # Invocar LLM
        raw = llm.invoke([sys_msg, user_msg]).content or ""
        # Tomar el primer bloque {...} como JSON
        m = re.search(r'\{[\s\S]*\}', raw)
        data = json.loads(m.group(0)) if m else {}
        sql  = (data.get("sql") or "").strip()
        mode = (data.get("mode") or "").strip().lower()

        # Si el prompt sugiere exportación, forzamos export aunque el modelo no lo ponga
        if looks_like_export_intent(prompt_completo):
            mode = "export"

        # Validación de SQL
        if not sql:
            return jsonify({"respuesta": "No pude generar una consulta SQL para tu pregunta."}), 400
        if not is_sql_safe(sql, user_empresa_id):
            return jsonify({"respuesta": "La consulta generada no está permitida por razones de seguridad."}), 400

        # Forzar filtro por empresa_id si no está
        sql = ensure_empresa_filter(sql, user_empresa_id)

        # En preview: fuerza LIMIT 1000; en export: NO agregues LIMIT
        if mode != "export":
            sql = add_limit(sql, MAX_PREVIEW_ROWS)
            return jsonify({
                "sql_code": sql,
                "mode": "preview",
                "message": f"Vista previa (hasta {MAX_PREVIEW_ROWS} filas). Solo generé el SQL; ejecuta en tu backend."
            })
        else:
            return jsonify({
                "sql_code": sql,
                "mode": "export",
                "message": "SQL listo para exportar en TU servidor. Aquí no se devolvieron filas."
            })

    except Exception as e:
        print(f"!!! ERROR INESPERADO EN EL SERVIDOR: {e}", file=sys.stderr)
        traceback.print_exc()
        return jsonify({"error": "Ocurrió un error inesperado en el servidor al procesar la solicitud."}), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
