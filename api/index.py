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
    
    main_meta = {'name': data['name'], 'mimeType': 'application/vnd.google-apps.folder', 'parents': [os.getenv("MASTER_FOLDER_ID")]}
    main_folder = drive.files().create(body=main_meta, fields='id', supportsAllDrives=True).execute()
    
    edit_meta = {'name': 'Edited', 'mimeType': 'application/vnd.google-apps.folder', 'parents': [main_folder['id']]}
    edited_folder = drive.files().create(body=edit_meta, fields='id', supportsAllDrives=True).execute()
    
    try:
        drive.permissions().create(fileId=main_folder['id'], body={'type': 'anyone', 'role': 'reader'}, supportsAllDrives=True).execute()
        drive.permissions().create(fileId=edited_folder['id'], body={'type': 'anyone', 'role': 'reader'}, supportsAllDrives=True).execute()
    except: pass
    
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO projects (client_name, selection_limit, folder_id, edited_folder_id, client_secret) VALUES (%s,%s,%s,%s,%s)",
                (data['name'], int(data.get('limit', 20)), main_folder['id'], edited_folder['id'], secret))
    conn.commit()
    return jsonify({"status": "success"})

@app.route('/api/sync-master-folders', methods=['POST'])
def sync_master():
    drive = get_drive()
    conn = get_db(); cur = conn.cursor()
    
    results = drive.files().list(q=f"'{os.getenv('MASTER_FOLDER_ID')}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false", fields="files(id, name)").execute()
    drive_folders = results.get('files', [])
    drive_ids = [f['id'] for f in drive_folders]
    
    cur.execute("SELECT folder_id, id FROM projects")
    db_folders = {row[0]: row[1] for row in cur.fetchall()}
    
    for f_id, p_id in db_folders.items():
        if f_id not in drive_ids:
            cur.execute("DELETE FROM projects WHERE id = %s", (p_id,))
            
    for f in drive_folders:
        if f['id'] not in db_folders:
            sub_res = drive.files().list(q=f"'{f['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false", fields="files(id, name)").execute()
            subfolders = sub_res.get('files', [])
            
            edited_id = None
            for sub in subfolders:
                if sub['name'].lower() == 'edited':
                    edited_id = sub['id']
                    break
            
            if not edited_id:
                edited_meta = {'name': 'Edited', 'mimeType': 'application/vnd.google-apps.folder', 'parents': [f['id']]}
                edited_folder = drive.files().create(body=edited_meta, fields='id', supportsAllDrives=True).execute()
                edited_id = edited_folder['id']
            
            try:
                drive.permissions().create(fileId=f['id'], body={'type': 'anyone', 'role': 'reader'}, supportsAllDrives=True).execute()
                drive.permissions().create(fileId=edited_id, body={'type': 'anyone', 'role': 'reader'}, supportsAllDrives=True).execute()
            except: pass

            secret = str(uuid.uuid4())[:8]
            cur.execute("INSERT INTO projects (client_name, selection_limit, folder_id, edited_folder_id, client_secret) VALUES (%s,%s,%s,%s,%s)", 
                        (f['name'], 20, f['id'], edited_id, secret))
    
    conn.commit()
    return jsonify({"status": "Two-Way Sync Complete!"})

@app.route('/api/delete-project', methods=['POST'])
def delete_project():
    p_id = request.json['p_id']
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT folder_id FROM projects WHERE id = %s", (p_id,))
    folder_id = cur.fetchone()
    
    if folder_id:
        drive = get_drive()
        master_id = os.getenv("MASTER_FOLDER_ID")
        try: drive.files().delete(fileId=folder_id[0], supportsAllDrives=True).execute()
        except: 
            try: drive.files().update(fileId=folder_id[0], removeParents=master_id, supportsAllDrives=True).execute()
            except: pass
            
    cur.execute("DELETE FROM projects WHERE id = %s", (p_id,))
    conn.commit()
    return jsonify({"status": "Project Deleted"})

@app.route('/api/update-limit', methods=['POST'])
def update_limit():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE projects SET selection_limit = %s WHERE id = %s", (int(data['limit']), data['p_id']))
    conn.commit()
    return jsonify({"status": "success"})

@app.route('/api/list-projects', methods=['GET'])
def list_p():
    archived = request.args.get('archived', 'false').lower() == 'true'
    conn = get_db(); cur = conn.cursor()
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
    conn = get_db(); cur = conn.cursor()
    
    target_id = f_id
    if is_edited:
        cur.execute("SELECT edited_folder_id FROM projects WHERE id = %s", (p_id,))
        target_id = cur.fetchone()[0]
    
    query = f"'{target_id}' in parents and (mimeType contains 'image/' or mimeType contains 'video/') and trashed = false"
    files = drive.files().list(q=query, fields="files(id, thumbnailLink, name, mimeType)").execute().get('files', [])
    
    if is_edited: cur.execute("UPDATE photos SET is_latest = FALSE WHERE project_id = %s AND is_edited = TRUE", (p_id,))
    
    added = 0
    for f in files:
        cur.execute("SELECT id FROM photos WHERE project_id = %s AND file_name = %s AND is_edited = %s", (p_id, f['name'], is_edited))
        if not cur.fetchone():
            cur.execute("INSERT INTO photos (project_id, drive_id, thumbnail_url, is_edited, file_name, mime_type) VALUES (%s,%s,%s,%s,%s,%s)", 
                        (p_id, f['id'], f.get('thumbnailLink', ''), is_edited, f['name'], f.get('mimeType', 'image/jpeg')))
            added += 1
    conn.commit()
    return jsonify({"status": "success", "added": added})

@app.route('/api/get-client-gallery', methods=['GET'])
def get_gallery():
    secret = request.args.get('secret')
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, client_name, selection_limit, edited_folder_id, folder_id, status FROM projects WHERE client_secret = %s AND is_archived = FALSE", (secret,))
    proj = cur.fetchone()
    if not proj: return jsonify({"error": "Link Inactive or Invalid"}), 404
    
    cur.execute("SELECT id, drive_id, thumbnail_url, is_edited, is_selected, file_name, mime_type FROM photos WHERE project_id = %s AND is_latest = TRUE", (proj[0],))
    photos = [{"db_id": r[0], "id": r[1], "url": r[2], "edited": r[3], "selected": r[4], "name": r[5], "mime_type": r[6]} for r in cur.fetchall()]
    return jsonify({"project": proj, "photos": photos})

@app.route('/api/toggle-selection', methods=['POST'])
def toggle_selection():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE photos SET is_selected = %s WHERE id = %s", (data['selected'], data['photo_id']))
    conn.commit()
    return jsonify({"status": "success"})

@app.route('/api/submit-selections', methods=['POST'])
def submit_selections():
    secret = request.json['secret']
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE projects SET status = 'submitted' WHERE client_secret = %s", (secret,))
    conn.commit()
    return jsonify({"status": "success"})

@app.route('/api/get-selections', methods=['GET'])
def get_selections():
    p_id = request.args.get('p_id')
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT file_name FROM photos WHERE project_id = %s AND is_selected = TRUE", (p_id,))
    return jsonify({"filenames": [r[0] for r in cur.fetchall()]})

@app.route('/api/archive-project', methods=['POST'])
def archive():
    p_id, val = request.json['p_id'], request.json.get('val', True)
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE projects SET is_archived = %s WHERE id = %s", (val, p_id))
    conn.commit()
    return jsonify({"status": "ok"})