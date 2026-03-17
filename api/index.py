from flask import Flask, request, jsonify
import os, psycopg2
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# --- Helper Functions ---
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def get_drive():
    info = {
        "private_key": os.getenv("GCP_PRIVATE_KEY").replace('\\n', '\n'),
        "client_email": os.getenv("GCP_SERVICE_ACCOUNT_EMAIL"),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    creds = service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
    return build('drive', 'v3', credentials=creds)

# --- Routes ---

# Home route to check if API is alive
@app.route('/api')
def hello():
    return jsonify({"status": "Gulugulu Pics API is Live"})

@app.route('/api/create-project', methods=['POST'])
def create_project():
    data = request.json
    name, limit = data['name'], data['limit']
    drive = get_drive()
    
    # 1. Create Folder
    meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [os.getenv("MASTER_FOLDER_ID")]}
    folder = drive.files().create(body=meta, fields='id').execute()
    
    # 2. Save Project
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO projects (client_name, selection_limit, folder_id) VALUES (%s, %s, %s) RETURNING id", 
                (name, limit, folder['id']))
    p_id = cur.fetchone()[0]
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"project_id": p_id, "folder_id": folder['id']})

@app.route('/api/get-gallery', methods=['GET'])
def get_gallery():
    p_id = request.args.get('id')
    conn = get_db(); cur = conn.cursor()
    
    cur.execute("SELECT client_name, selection_limit, folder_id FROM projects WHERE id = %s", (p_id,))
    proj = cur.fetchone()
    
    cur.execute("SELECT drive_id, thumbnail_url, is_selected FROM photos WHERE project_id = %s", (p_id,))
    photos = [{"drive_id": r[0], "thumb": r[1], "selected": r[2]} for r in cur.fetchall()]
    
    cur.close(); conn.close()
    return jsonify({
        "client_name": proj[0], "limit": proj[1], "folder_id": proj[2], "photos": photos
    })

@app.route('/api/sync-folder', methods=['POST'])
def sync_folder():
    data = request.json
    p_id, f_id = data['project_id'], data['folder_id']
    drive = get_drive()
    
    # Get all images in that drive folder
    query = f"'{f_id}' in parents and mimeType contains 'image/'"
    results = drive.files().list(q=query, fields="files(id, thumbnailLink)").execute()
    files = results.get('files', [])

    conn = get_db(); cur = conn.cursor()
    for f in files:
        cur.execute("SELECT id FROM photos WHERE drive_id = %s", (f['id'],))
        if not cur.fetchone():
            cur.execute("INSERT INTO photos (project_id, drive_id, thumbnail_url) VALUES (%s, %s, %s)", 
                        (p_id, f['id'], f['thumbnailLink']))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "synced", "count": len(files)})

@app.route('/api/submit-selection', methods=['POST'])
def submit():
    data = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE photos SET is_selected = FALSE WHERE project_id = %s", (data['project_id'],))
    for d_id in data['selections']:
        cur.execute("UPDATE photos SET is_selected = TRUE WHERE drive_id = %s", (d_id,))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "success"})

# IMPORTANT: Remove the handler function and just expose 'app'
# Vercel will look for 'app' automatically