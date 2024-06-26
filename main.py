import json
from flask import Flask, redirect, url_for, session, render_template
from flask import request, jsonify
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import os
import google.oauth2.credentials
import secrets
import firebase_admin
import google.generativeai as genai
from firebase_admin import credentials, db

if os.getenv('API_ENV') != 'production':
    from dotenv import load_dotenv

    load_dotenv()


app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# 设置 OAuth 2.0 客户端 ID 文件路径
CLIENT_SECRETS_FILE = "client_secret.json"

# 设置 API 访问范围
SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.readonly"
]

# 设置重定向 URI
REDIRECT_URI = 'http://localhost:5000/oauth2callback'

# 初始化 Firebase Admin SDK
cred = credentials.Certificate('service_key.json')
firebase_admin.initialize_app(cred, {
    'databaseURL': os.getenv('FIREBASE_URL')
})

gemini_key = os.getenv('GEMINI_API_KEY')

# Initialize the Gemini Pro API
genai.configure(api_key=gemini_key)


def save_youtube_live_id_to_firebase(live_chat_id):
    session['live_chat_id'] = live_chat_id
    ref = db.reference('live_chat_id')
    ref.set(live_chat_id)
    return True


def save_credentials_to_firebase(credentials):
    session['credentials'] = credentials_to_dict(credentials)
    ref = db.reference('credentials')
    ref.set(credentials_to_dict(credentials))
    return True


def write_firebase_credentials_to_session():
    ref = db.reference('credentials')
    ref_live = db.reference('live_chat_id')
    credentials = ref.get()
    live_id = ref_live.get()
    if credentials and live_id:
        session['credentials'] = credentials
        session['live_chat_id'] = live_id
        return True
    return False


@app.route('/root')
def index():
    return '''
        <h1>Welcome to YouTube API with Flask!</h1>
        <a href="#" onclick="authorize('live')">Authorize to list live broadcasts</a><br>
        <a href="#" onclick="authorize('list')">Authorize to list live chat messages</a>
        <a href="#" onclick="authorize('check')">Authorize to moderate chat messages</a>
        <script>
            function authorize(target) {
                localStorage.setItem('oauth_target', target);
                window.location.href = '/authorize';
            }
        </script>
    '''


@app.route('/authorize')
def authorize():
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    session['state'] = state
    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    state = session['state']
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        state=state,
        redirect_uri=REDIRECT_URI
    )
    flow.fetch_token(authorization_response=request.url)

    save_credentials_to_firebase(flow.credentials)
    write_firebase_credentials_to_session()

    return '''
        <script>
            var target = localStorage.getItem('oauth_target');
            localStorage.removeItem('oauth_target');
            if (target) {
                window.location.href = '/' + target;
            } else {
                window.location.href = '/';
            }
        </script>
    '''


@app.route('/')
def list_live_broadcasts():
    if 'credentials' not in session:
        return redirect(url_for('authorize'))

    credentials = google.oauth2.credentials.Credentials(
        **session['credentials']
    )
    youtube = build('youtube', 'v3', credentials=credentials)

    request = youtube.liveBroadcasts().list(
        part="snippet,contentDetails,status",
        broadcastStatus="active"
    )
    response = request.execute()

    if not response['items']:
        return 'No active live broadcasts found.'

    live_broadcast = response['items'][0]
    live_chat_id = live_broadcast['snippet'].get('liveChatId')

    if live_chat_id:
        session['live_chat_id'] = live_chat_id
        save_youtube_live_id_to_firebase(live_chat_id)
        return '''
            <h1>Welcome to YouTube API with Flask!</h1>
            <a href="#" onclick="authorize('list')">Authorize to list live chat messages</a>
            <a href="#" onclick="authorize('check')">Authorize to moderate chat messages</a>
            <script>
                function authorize(target) {
                    localStorage.setItem('oauth_target', target);
                    window.location.href = '/authorize';
                }
            </script>
        '''
    else:
        return 'No live chat available for this broadcast.'


@app.route('/list')
def list_live_chat_messages():
    write_firebase_credentials_to_session()
    if 'credentials' not in session:
        return redirect(url_for('authorize'))
    if 'live_chat_id' not in session:
        return 'No live_chat_id in session. Please ensure you have an active live broadcast with chat enabled.'

    return render_template('chat.html')


@app.route('/get_live_chat_messages')
def get_live_chat_messages():
    write_firebase_credentials_to_session()
    if 'credentials' not in session:
        return jsonify({'error': 'Not authorized'}), 401
    if 'live_chat_id' not in session:
        return jsonify({'error': 'No live_chat_id in session'}), 400

    credentials = google.oauth2.credentials.Credentials(
        **session['credentials']
    )
    youtube = build('youtube', 'v3', credentials=credentials)

    live_chat_id = session['live_chat_id']
    next_page_token = session.get('next_page_token')

    request = youtube.liveChatMessages().list(
        liveChatId=live_chat_id,
        part="snippet,authorDetails",
        pageToken=next_page_token
    )
    response = request.execute()

    messages = response.get('items', [])
    session['next_page_token'] = response.get('nextPageToken')

    print(messages)
    # 将消息存储到 Firebase Realtime Database
    ref = db.reference('messages')
    for message in messages:
        ref.push({
            "displayName": message["authorDetails"]["displayName"],
            "isChatSponsor": message["authorDetails"]["isChatSponsor"],
            "displayMessage": message["snippet"]["displayMessage"],
            "liveChatId": message["snippet"]["liveChatId"],
            "publishedAt": message["snippet"]["publishedAt"]
        })

    return jsonify(messages)


@app.route('/check')
def moderate_chat_messages():
    write_firebase_credentials_to_session()
    if 'credentials' not in session:
        return redirect(url_for('authorize'))
    if 'live_chat_id' not in session:
        return 'No live_chat_id in session. Please ensure you have an active live broadcast with chat enabled.'

    credentials = google.oauth2.credentials.Credentials(
        **session['credentials']
    )
    youtube = build('youtube', 'v3', credentials=credentials)

    ref = db.reference('live_chat_id')
    live_chat_id = str(ref.get())
    messages = get_messages_from_firebase()
    # 检查关键字并采取行动
    # for message in messages:

    #     message_text = message['snippet']['displayMessage']
    #     print(message_text)
    #     author_channel_id = message['authorDetails']['channelId']
    #     message_id = message['id']

    model = genai.GenerativeModel('gemini-pro')
    response = model.generate_content(
        f'你現在是一個英雄聯盟的遊戲直播主，請針對以下的內容，用一句話回覆用戶內容。\n{messages}')
    print(response.text)
    # 回复用户
    youtube.liveChatMessages().insert(
        part='snippet',
        body={
            'snippet': {
                'liveChatId': live_chat_id,
                'type': 'textMessageEvent',
                'textMessageDetails': {
                    'messageText': response.text
                }
            }
        }
    ).execute()

    return jsonify({'status': 'Moderation actions completed'})


@app.route('/fetch_messages_from_firebase')
def fetch_messages_from_firebase():
    ref = db.reference('messages')
    messages = ref.get()
    return jsonify(messages)


def get_messages_from_firebase():
    ref = db.reference('messages')
    messages = ref.get()

    return str(messages)


def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }


if __name__ == '__main__':
    app.run('localhost', 5000, debug=True)
