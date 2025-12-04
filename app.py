# app.py
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
import mysql.connector
import traceback
import config
from datetime import date

app = Flask(__name__)
app.config.from_object('config')
app.secret_key = config.SECRET_KEY
CORS(app)

# --- Database helper ---
def get_db():
    return mysql.connector.connect(**config.db_config)

def query_one(q, params=None):
    cnx = get_db()
    cur = cnx.cursor(dictionary=True)
    cur.execute(q, params or ())
    row = cur.fetchone()
    cur.close()
    cnx.close()
    return row

def query_all(q, params=None):
    cnx = get_db()
    cur = cnx.cursor(dictionary=True)
    cur.execute(q, params or ())
    rows = cur.fetchall()
    cur.close()
    cnx.close()
    return rows

def exec_stmt(q, params=None, commit=True):
    cnx = get_db()
    cur = cnx.cursor()
    cur.execute(q, params or ())
    lastid = cur.lastrowid
    if commit:
        cnx.commit()
    cur.close()
    cnx.close()
    return lastid

# --- Helpers ---
def make_token_no(prefix, dept_id):
    # Simple token format: PREFIX-<suffix>-YYYYMMDD
    # Use DB function next_token_suffix to get a per-dept per-day suffix
    cnx = get_db()
    cur = cnx.cursor()
    cur.execute("SELECT next_token_suffix(%s, CURDATE())", (dept_id,))
    row = cur.fetchone()
    suffix = row[0] if row else 1
    token_no = f"{prefix}-{str(suffix).zfill(3)}-{date.today().strftime('%Y%m%d')}"
    cur.close()
    cnx.close()
    return token_no

# --- Pages (renders) ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/staff")
def staff_page():
    # minimal demo; staff login page handles auth
    return render_template("staff_login.html")

@app.route("/staff/dashboard")
def staff_dashboard_page():
    # require staff login (demo)
    if 'user_id' not in session:
        return redirect(url_for('staff_page'))
    return render_template("staff_dashboard.html")

# -----------------------
# API endpoints
# -----------------------

# Create token (patient)

@app.route("/api/token", methods=["POST"])
def create_token():
    """
    Robust token creation:
      - computes a per-dept-per-day suffix inside a transaction (SELECT ... FOR UPDATE)
      - attempts INSERT; on duplicate (rare) it retries with incremented suffix
    """
    try:
        data = request.json
        patient_name = data.get("patient_name")
        patient_phone = data.get("patient_phone")
        dept_id = int(data.get("dept_id"))
        appointment_date = data.get("appointment_date")  # YYYY-MM-DD
        if not appointment_date:
            appointment_date = date.today().isoformat()
        priority_requested = 1 if data.get("priority_requested") else 0
        reason = data.get("reason") or "Visit"

        # get dept prefix
        dept = query_one("SELECT abbr FROM departments WHERE id=%s", (dept_id,))
        if not dept:
            return jsonify({"status":"error","message":"Invalid department"}), 400
        prefix = dept["abbr"] or "TKN"

        # We'll attempt to insert in a loop; usually succeeds on first try.
        max_attempts = 8
        attempt = 0
        created_token = None

        while attempt < max_attempts and created_token is None:
            attempt += 1
            cnx = get_db()
            cur = cnx.cursor()
            try:
                # Start transaction
                cnx.start_transaction()

                # Compute a suffix in a transactional way:
                # get current max id for dept+date then add 1 (FOR UPDATE to reduce races)
                cur.execute(
                    "SELECT COALESCE(MAX(id),0) + 1 FROM tokens WHERE dept_id = %s AND appointment_date = %s FOR UPDATE",
                    (dept_id, appointment_date)
                )
                row = cur.fetchone()
                suffix = row[0] if row and row[0] is not None else 1

                token_no = f"{prefix}-{str(suffix).zfill(3)}-{date.today().strftime('%Y%m%d')}"

                # Try to insert
                cur.execute(
                    "INSERT INTO tokens (token_no, patient_name, patient_phone, dept_id, appointment_date, priority_requested, reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (token_no, patient_name, patient_phone, dept_id, appointment_date, priority_requested, reason)
                )

                cnx.commit()
                created_token = token_no
            except mysql.connector.IntegrityError as ie:
                # Duplicate key — someone else just created the same token_no
                cnx.rollback()
                # increment suffix and retry by manually increasing the "id-based" suffix assumption
                # next loop will re-query MAX(id)+1 and try again (or we increment local suffix and try insert directly)
                # As a small optimization, try inserting with incremented local suffix once:
                try:
                    suffix += 1
                    token_no = f"{prefix}-{str(suffix).zfill(3)}-{date.today().strftime('%Y%m%d')}"
                    cur.execute(
                        "INSERT INTO tokens (token_no, patient_name, patient_phone, dept_id, appointment_date, priority_requested, reason) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (token_no, patient_name, patient_phone, dept_id, appointment_date, priority_requested, reason)
                    )
                    cnx.commit()
                    created_token = token_no
                    break
                except mysql.connector.IntegrityError:
                    cnx.rollback()
                    # will loop again and recompute suffix
            except Exception as ex:
                cnx.rollback()
                cur.close()
                cnx.close()
                raise ex
            finally:
                try:
                    cur.close()
                    cnx.close()
                except:
                    pass

        if created_token is None:
            return jsonify({"status":"error","message":"Could not generate unique token, please try again"}), 500

        # compute position and ETA (same logic as before)
        pos_eta = query_one("""
            SELECT
              (
                SELECT COUNT(*) FROM tokens tt
                WHERE tt.dept_id = t.dept_id
                  AND tt.appointment_date = t.appointment_date
                  AND tt.status = 'Waiting'
                  AND (
                       tt.priority_approved > t.priority_approved
                       OR (tt.priority_approved = t.priority_approved AND tt.created_at < t.created_at)
                  )
              ) AS position_ahead,
              (
                SELECT COALESCE(d.avg_service_time,5) FROM departments d WHERE d.id = t.dept_id
              ) * (
                SELECT COUNT(*) FROM tokens tt
                WHERE tt.dept_id = t.dept_id
                  AND tt.appointment_date = t.appointment_date
                  AND tt.status = 'Waiting'
                  AND (
                       tt.priority_approved > t.priority_approved
                       OR (tt.priority_approved = t.priority_approved AND tt.created_at < t.created_at)
                  )
              ) AS estimated_wait_minutes
            FROM tokens t
            WHERE t.token_no = %s
        """, (created_token,))

        return jsonify({
            "status": "success",
            "token_no": created_token,
            "position_ahead": pos_eta["position_ahead"] if pos_eta else 0,
            "estimated_wait_minutes": pos_eta["estimated_wait_minutes"] if pos_eta else 0
        }), 201

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status":"error", "message": str(e)}), 500

# Get token status (polling)
@app.route("/api/token/<token_no>", methods=["GET"])
def get_token(token_no):
    t = query_one("SELECT token_no, patient_name, patient_phone, dept_id, appointment_date, priority_requested, priority_approved, status, created_at, called_at, completed_at FROM tokens WHERE token_no = %s", (token_no,))
    if not t:
        return jsonify({"status":"error","message":"Token not found"}), 404

    # compute position and ETA using same query as above
    pos_eta = query_one("""
        SELECT
          (
            SELECT COUNT(*) FROM tokens tt
            WHERE tt.dept_id = t.dept_id
              AND tt.appointment_date = t.appointment_date
              AND tt.status = 'Waiting'
              AND (
                   tt.priority_approved > t.priority_approved
                   OR (tt.priority_approved = t.priority_approved AND tt.created_at < t.created_at)
              )
          ) AS position_ahead,
          (
            SELECT COALESCE(d.avg_service_time,5) FROM departments d WHERE d.id = t.dept_id
          ) * (
            SELECT COUNT(*) FROM tokens tt
            WHERE tt.dept_id = t.dept_id
              AND tt.appointment_date = t.appointment_date
              AND tt.status = 'Waiting'
              AND (
                   tt.priority_approved > t.priority_approved
                   OR (tt.priority_approved = t.priority_approved AND tt.created_at < t.created_at)
              )
          ) AS estimated_wait_minutes
        FROM tokens t
        WHERE t.token_no = %s
    """, (token_no,))

    return jsonify({"status":"success", "token": t, "position_ahead": pos_eta["position_ahead"], "estimated_wait_minutes": pos_eta["estimated_wait_minutes"]})

# Patient cancel token
@app.route("/api/token/<token_no>/cancel", methods=["PUT"])
def cancel_token(token_no):
    try:
        # Allow cancellation only if status is Waiting or Called
        rows = exec_stmt("UPDATE tokens SET status='Cancelled', cancelled_at=NOW(), cancelled_by='patient' WHERE token_no=%s AND status IN ('Waiting','Called')", (token_no,))
        if rows == 0:
            return jsonify({"status":"error","message":"Cannot cancel (maybe already in-progress/completed)"}), 400
        # insert log
        exec_stmt("INSERT INTO logs (token_id, event, actor) VALUES ((SELECT id FROM tokens WHERE token_no=%s), 'cancelled', 'patient')", (token_no,))
        return jsonify({"status":"success","message":"Cancelled"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status":"error","message":str(e)}), 500

# Staff: simple login (demo)
@app.route("/api/staff/login", methods=["POST"])
def staff_login():
    data = request.json
    email = data.get("email")
    # For hackathon: If password is set in DB you should verify it. Here we accept email only for demo.
    user = query_one("SELECT id, name, role, department_id FROM users WHERE email=%s AND is_active=1", (email,))
    if not user:
        return jsonify({"status":"error","message":"Invalid staff email"}), 401
    session['user_id'] = user['id']
    session['role'] = user['role']
    session['name'] = user['name']
    return jsonify({"status":"success","user":user})

# Staff logout
@app.route("/api/staff/logout", methods=["POST"])
def staff_logout():
    session.clear()
    return jsonify({"status":"success"})

# Staff: Get tokens for a dept + date
@app.route("/api/staff/tokens", methods=["GET"])
def staff_get_tokens():
    if 'user_id' not in session:
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    dept_id = request.args.get("dept_id")
    appt_date = request.args.get("date") or date.today().isoformat()
    if not dept_id:
        return jsonify({"status":"error","message":"dept_id required"}), 400

    rows = query_all("""
        SELECT id, token_no, patient_name, patient_phone, priority_requested, priority_approved, status, created_at
        FROM tokens
        WHERE dept_id=%s AND appointment_date=%s
        ORDER BY priority_approved DESC, created_at ASC
    """, (dept_id, appt_date))

    return jsonify({"status":"success","tokens": rows})

# Staff: Approve priority
@app.route("/api/token/<token_no>/approve", methods=["PUT"])
def approve_priority(token_no):
    if 'user_id' not in session:
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    # only allow if requested
    exec_stmt("UPDATE tokens SET priority_approved=1 WHERE token_no=%s AND priority_requested=1", (token_no,))
    exec_stmt("INSERT INTO logs (token_id, event, actor) VALUES ((SELECT id FROM tokens WHERE token_no=%s), 'approved_priority', %s)", (token_no, session.get('name')))
    return jsonify({"status":"success","message":"Priority approved"})

# Staff: Mark status (In-Progress, Completed, No-show)
@app.route("/api/token/<token_no>/status", methods=["PUT"])
def update_status(token_no):
    if 'user_id' not in session:
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    data = request.json
    new_status = data.get("status")
    if new_status not in ["In-Progress","Completed","No-show"]:
        return jsonify({"status":"error","message":"Invalid status"}), 400
    if new_status == "Completed":
        exec_stmt("UPDATE tokens SET status='Completed', completed_at=NOW() WHERE token_no=%s AND status IN ('Called','In-Progress')", (token_no,))
    else:
        exec_stmt("UPDATE tokens SET status=%s WHERE token_no=%s", (new_status, token_no))
    exec_stmt("INSERT INTO logs (token_id, event, actor) VALUES ((SELECT id FROM tokens WHERE token_no=%s), %s, %s)", (token_no, new_status, session.get('name')))
    return jsonify({"status":"success","message":"Status updated"})

# -------------------------
# Admin management endpoints
# -------------------------

# Who am I (session info) - frontend uses this to show admin tab
@app.route("/api/me", methods=["GET"])
def api_me():
    if 'user_id' not in session:
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    return jsonify({
        "status":"success",
        "user": {
            "id": session.get('user_id'),
            "name": session.get('name'),
            "role": session.get('role')
        }
    })

# Admin: get all users
@app.route("/api/admin/users", methods=["GET"])
def admin_get_users():
    if 'user_id' not in session or session.get('role') != 'admin':
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    rows = query_all("SELECT id, name, email, phone, role, department_id, is_active FROM users ORDER BY id")
    return jsonify({"status":"success","users": rows})

# Admin: add user
@app.route("/api/admin/users", methods=["POST"])
def admin_add_user():
    if 'user_id' not in session or session.get('role') != 'admin':
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    data = request.json
    name = data.get("name")
    email = data.get("email")
    phone = data.get("phone")
    department_id = data.get("department_id")
    role = data.get("role") or "staff"
    # simple create - production: check duplicates & hash password
    exec_stmt("INSERT INTO users (name, email, phone, role, department_id, is_active) VALUES (%s,%s,%s,%s,%s,1)",
              (name, email, phone, role, department_id))
    return jsonify({"status":"success","message":"User created"})

# Admin: update user
@app.route("/api/admin/users/<int:user_id>", methods=["PUT"])
def admin_update_user(user_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    data = request.json
    name = data.get("name")
    email = data.get("email")
    phone = data.get("phone")
    department_id = data.get("department_id")
    role = data.get("role")
    is_active = 1 if data.get("is_active") in (1, "1", True, "true") else 0
    exec_stmt("""
        UPDATE users SET name=%s, email=%s, phone=%s, department_id=%s, role=%s, is_active=%s
        WHERE id=%s
    """, (name, email, phone, department_id, role, is_active, user_id))
    return jsonify({"status":"success","message":"User updated"})


# Staff: Call next (uses stored procedure)
@app.route("/api/departments/<int:dept_id>/call-next", methods=["PUT"])
def call_next(dept_id):
    if 'user_id' not in session:
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    appt_date = request.json.get("date") or date.today().isoformat()
    cnx = get_db()
    cur = cnx.cursor()
    out_token = None
    try:
        # call stored proc
        cur.callproc('call_next_token', [dept_id, appt_date, session.get('name') or 'staff', 0])
        # MySQL python returns results in cursor.stored_results() sometimes; read user variable instead
        # read OUT param via SELECT @out parameter not available here; instead execute SELECT for last log token
        # Simpler approach: after proc, select the most recent 'Called' token for dept/date with recent called_at
        cnx.commit()
        cur.close()
        token = query_one("""
            SELECT token_no, status, called_at FROM tokens
            WHERE dept_id=%s AND appointment_date=%s AND status='Called'
            ORDER BY called_at DESC LIMIT 1
        """, (dept_id, appt_date))
        return jsonify({"status":"success","called_token": token})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status":"error","message":str(e)}), 500
    finally:
        try:
            cur.close()
            cnx.close()
        except:
            pass

# Admin: List all tokens (for admin dashboard)
@app.route("/api/admin/tokens", methods=["GET"])
def admin_tokens():
    if 'user_id' not in session or session.get('role') != 'admin':
        return jsonify({"status":"error","message":"Unauthorized"}), 401
    appt_date = request.args.get("date") or date.today().isoformat()
    rows = query_all("SELECT * FROM tokens WHERE appointment_date=%s ORDER BY dept_id, created_at", (appt_date,))
    return jsonify({"status":"success","tokens": rows})

# Run
if __name__ == "__main__":
    app.run(debug=True, port=5000)
