from flask import Flask, request, jsonify
import os, psycopg2, uuid, textwrap
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"), sslmode='require')

def get_drive():
    raw_key = os.getenv("GCP_PRIVATE_KEY", "").replace('\\n', '\n').strip('"').strip("'")
    if "-----BEGIN PRIVATE KEY-----" in raw_key and "\n" not in raw_key.replace("-----BEGIN PRIVATE KEY-----", "").strip():
        body = raw_key.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "").strip()
        raw_key = f"-----BEGIN PRIVATE KEY-----\n{textwrap.fill(body, 64)}\n-----END PRIVATE KEY-----"
    info = {"private_key": raw_key, "client_email": os.getenv("GCP_SERVICE_ACCOUNT_EMAIL"), "token_uri": "https://oauth2.googleapis.com/token"}
    creds = service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
    return build('drive', 'v3', credentials=creds)

@app.route('/api/create-project', methods=['POST'])
def create():
    data = request.json
    secret = str(uuid.uuid4())[:8]
    drive = get_drive()
    meta = {'name': data['name'], 'mimeType': 'application/vnd.google-apps.folder', 'parents': [os.getenv("MASTER_FOLDER_ID")]}
    folder = drive.files().create(body=meta, fields='id', supportsAllDrives=True).execute()
    
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO projects (client_name, selection_limit, folder_id, client_secret) VALUES (%s,%s,%s,%s) RETURNING id",
                (data['name'], int(data.get('limit', 20)), folder['id'], secret))
    conn.commit()
    return jsonify({"status": "success"})

@app.route('/api/list-projects', methods=['GET'])
def list_p():
    archived = request.args.get('archived', 'false').lower() == 'true'
    conn = get_db(); cur = conn.cursor()
    # Also fetch how many photos are currently selected
    cur.execute("""
        SELECT p.id, p.client_name, p.status, p.client_secret, p.folder_id, p.selection_limit,
               (SELECT COUNT(*) FROM photos WHERE project_id = p.id AND is_selected = TRUE AND is_edited = FALSE) as selected_count
        FROM projects p WHERE is_archived = %s ORDER BY created_at DESC
    """, (archived,))
    rows = cur.fetchall()
    return jsonify([{"id":r[0],"name":r[1],"status":r[2],"secret":r[3],"folder":r[4],"limit":r[5],"selected":r[6]} for r in rows])

@app.route('/api/sync-drive', methods=['POST'])
def sync():
    data = request.json
    p_id, f_id, is_edited = data['p_id'], data['f_id'], data.get('is_edited', False)
    drive = get_drive()
    target_id = f_id
    if is_edited:
        res = drive.files().list(q=f"'{f_id}' in parents and name='Edited'").execute()
        if not res['files']: return jsonify({"error": "No Edited folder"}), 400
        target_id = res['files'][0]['id']
    
    files = drive.files().list(q=f"'{target_id}' in parents and mimeType contains 'image/'", fields="files(id, thumbnailLink, name)").execute().get('files', [])
    conn = get_db(); cur = conn.cursor()
    if is_edited: cur.execute("UPDATE photos SET is_latest = FALSE WHERE project_id = %s AND is_edited = TRUE", (p_id,))
    
    count = 0
    for f in files:
        cur.execute("SELECT id FROM photos WHERE drive_id = %s", (f['id'],))
        if not cur.fetchone():
            cur.execute("INSERT INTO photos (project_id, drive_id, thumbnail_url, is_edited, file_name) VALUES (%s,%s,%s,%s,%s)", 
                        (p_id, f['id'], f['thumbnailLink'], is_edited, f['name']))
            count += 1
    conn.commit()
    return jsonify({"status": "success", "added": count})

@app.route('/api/get-client-gallery', methods=['GET'])
def get_gallery():
    secret = request.args.get('secret')
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, client_name, selection_limit FROM projects WHERE client_secret = %s", (secret,))
    proj = cur.fetchone()
    if not proj: return jsonify({"error": "Invalid Link"}), 404
    
    cur.execute("SELECT id, drive_id, thumbnail_url, is_edited, is_selected, file_name FROM photos WHERE project_id = %s AND is_latest = TRUE", (proj[0],))
    photos = [{"db_id": r[0], "id": r[1], "url": r[2], "edited": r[3], "selected": r[4], "name": r[5]} for r in cur.fetchall()]
    return jsonify({"project": proj, "photos": photos})

@app.route('/api/toggle-selection', methods=['POST'])
def toggle_selection():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    # Verify secret matches project
    cur.execute("SELECT id FROM projects WHERE client_secret = %s", (data['secret'],))
    if not cur.fetchone(): return jsonify({"error": "Unauthorized"}), 401
    
    cur.execute("UPDATE photos SET is_selected = %s WHERE id = %s", (data['selected'], data['photo_id']))
    conn.commit()
    return jsonify({"status": "success"})

@app.route('/api/get-selections', methods=['GET'])
def get_selections():
    p_id = request.args.get('p_id')
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT file_name FROM photos WHERE project_id = %s AND is_selected = TRUE", (p_id,))
    files = [r[0] for r in cur.fetchall()]
    return jsonify({"filenames": files})

@app.route('/api/archive-project', methods=['POST'])
def archive():
    p_id, val = request.json['p_id'], request.json.get('val', True)
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE projects SET is_archived = %s WHERE id = %s", (val, p_id))
    conn.commit()
    return jsonify({"status": "ok"})