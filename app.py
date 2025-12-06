from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app
from datetime import datetime
import hashlib
import os
from functools import wraps

app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app)#cross origin resources source

# Initialize Firebase - Make sure 'firebase-credentials.json' is in the same directory
try:
    # Make sure to replace 'firebase-credentials.json' with your actual file name
    service_account_info = {
        "type": os.getenv("FIREBASE_TYPE"),
        "project_id": os.getenv("FIREBASE_PROJECT_ID"),
        "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
        "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace("\\n", "\n"),
        "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
        "client_id": os.getenv("FIREBASE_CLIENT_ID"),
        "auth_uri": os.getenv("FIREBASE_AUTH_URI"),
        "token_uri": os.getenv("FIREBASE_TOKEN_URI"),
        "auth_provider_x509_cert_url": os.getenv("FIREBASE_AUTH_PROVIDER_X509_CERT_URL"),
        "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_X509_CERT_URL"),
        "universe_domain": os.getenv("FIREBASE_UNIVERSE_DOMAIN"),
    }
    cred = credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    print(f"Error initializing Firebase: {e}")
    print("Please ensure 'firebase-credentials.json' is correctly configured and in the same directory.")
    db = None

# Admin credentials
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123" # Change this in production!

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({'error': 'Unauthorized. Admin access required.'}), 401
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.json
    if data.get('username') == ADMIN_USERNAME and data.get('password') == ADMIN_PASSWORD:
        session['admin_logged_in'] = True
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_logged_in', None)
    return jsonify({'success': True})

@app.route('/api/verify-aadhar', methods=['POST'])
def verify_aadhar():
    data = request.json
    aadhar_no = data.get('aadhar_no', '').strip()

    if not aadhar_no or len(aadhar_no) != 12 or not aadhar_no.isdigit():
        return jsonify({'error': 'Invalid Aadhar number'}), 400

    aadhar_hash = hashlib.sha256(aadhar_no.encode()).hexdigest()

    voters_ref = db.collection('voters')
    # Use where() to find the document by aadhar_hash
    query = voters_ref.where('aadhar_hash', '==', aadhar_hash).limit(1).get()

    if query:
        doc = query[0]
        voter_data = doc.to_dict()
        if voter_data.get('has_voted'):
            return jsonify({'error': 'This Aadhar has already been used to vote'}), 400
        voter_id = doc.id
    else:
        # Create a new voter record
        voter_id = aadhar_hash[:20] # Use a portion of the hash as a unique ID
        voters_ref.document(voter_id).set({
            'aadhar_hash': aadhar_hash,
            'has_voted': False,
            'created_at': datetime.now()
        })

    return jsonify({
        'success': True,
        'voter_id': voter_id,
        'message': 'Aadhar verified successfully'
    })


@app.route('/api/register-candidate', methods=['POST'])
@admin_required # Only admins can register candidates
def register_candidate():
    data = request.json

    candidate = {
        'name': data.get('name'),
        'party': data.get('party'),
        'photo_url': data.get('photo_url'),
        'manifesto': data.get('manifesto', ''),
        'vote_count': 0,
        'created_at': datetime.now()
    }

    candidates_ref = db.collection('candidates')
    doc_ref = candidates_ref.add(candidate)

    return jsonify({
        'success': True,
        'candidate_id': doc_ref[1].id,
        'message': 'Candidate registered successfully'
    })

@app.route('/api/candidates', methods=['GET'])
def get_candidates():
    candidates_ref = db.collection('candidates').order_by('name')
    candidates = []

    for doc in candidates_ref.stream():
        candidate = doc.to_dict()
        candidate['id'] = doc.id
        if 'created_at' in candidate and isinstance(candidate['created_at'], datetime):
            candidate['created_at'] = candidate['created_at'].isoformat()
        candidates.append(candidate)

    return jsonify(candidates)

@app.route('/api/vote', methods=['POST'])
def vote():
    data = request.json
    voter_id = data.get('voter_id')
    candidate_id = data.get('candidate_id')

    if not voter_id or not candidate_id:
        return jsonify({'error': 'Missing required data'}), 400

    voter_ref = db.collection('voters').document(voter_id)
    voter = voter_ref.get()

    if not voter.exists:
        return jsonify({'error': 'Voter not found'}), 404

    voter_data = voter.to_dict()
    if voter_data.get('has_voted'):
        return jsonify({'error': 'Already voted'}), 400

    # Check if voting is enabled in settings
    settings_ref = db.collection('settings').document('election')
    settings = settings_ref.get()
    if settings.exists and not settings.to_dict().get('voting_enabled', True):
        return jsonify({'error': 'Voting is currently disabled by Admin.'}), 403

    vote_record = {
        'voter_id': voter_id,
        'candidate_id': candidate_id,
        'timestamp': datetime.now()
    }

    db.collection('votes').add(vote_record)

    voter_ref.update({'has_voted': True, 'voted_at': datetime.now()})

    # Use a transaction to safely increment the vote count
    candidate_ref = db.collection('candidates').document(candidate_id)
    
    @firestore.transactional
    def update_in_transaction(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)
        new_votes = snapshot.get('vote_count') + 1
        transaction.update(doc_ref, {'vote_count': new_votes})

    transaction = db.transaction()
    update_in_transaction(transaction, candidate_ref)

    return jsonify({
        'success': True,
        'message': 'Vote recorded successfully'
    })

@app.route('/api/results', methods=['GET'])
def get_results():
    settings_ref = db.collection('settings').document('election')
    settings = settings_ref.get()

    show_results = False
    if settings.exists:
        settings_data = settings.to_dict()
        show_results = settings_data.get('show_results', False)

    if not show_results and not session.get('admin_logged_in'):
        return jsonify({'error': 'Results not yet available'}), 403

    candidates_ref = db.collection('candidates').order_by('vote_count', direction=firestore.Query.DESCENDING)
    results = []

    for doc in candidates_ref.stream():
        candidate = doc.to_dict()
        results.append({
            'id': doc.id,
            'name': candidate.get('name'),
            'party': candidate.get('party'),
            'vote_count': candidate.get('vote_count', 0),
            'photo_url': candidate.get('photo_url')
        })

    return jsonify(results)

@app.route('/api/admin/settings', methods=['GET', 'POST'])
@admin_required
def election_settings():
    settings_ref = db.collection('settings').document('election')

    if request.method == 'GET':
        settings = settings_ref.get()
        if settings.exists:
            data = settings.to_dict()
            if 'results_reveal_time' in data and data['results_reveal_time']:
                data['results_reveal_time'] = data['results_reveal_time'].isoformat()
            return jsonify(data)
        return jsonify({
            'show_results': False,
            'voting_enabled': True,
            'results_reveal_time': None
        })

    elif request.method == 'POST':
        data = request.json
        settings = {
            'show_results': data.get('show_results', False),
            'voting_enabled': data.get('voting_enabled', True),
            'updated_at': datetime.now()
        }

        if data.get('results_reveal_time'):
            # The input from datetime-local is already in ISO format
            settings['results_reveal_time'] = datetime.fromisoformat(data['results_reveal_time'])
        else:
            settings['results_reveal_time'] = None

        settings_ref.set(settings, merge=True)
        return jsonify({'success': True})

@app.route('/api/admin/delete-candidate/<candidate_id>', methods=['DELETE'])
@admin_required
def delete_candidate(candidate_id):
    try:
        db.collection('candidates').document(candidate_id).delete()
        return jsonify({'success': True, 'message': 'Candidate deleted.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    total_voters = len(list(db.collection('voters').stream()))
    total_votes = len(list(db.collection('votes').stream()))
    total_candidates = len(list(db.collection('candidates').stream()))

    return jsonify({
        'total_voters': total_voters,
        'total_votes': total_votes,
        'total_candidates': total_candidates,
        'turnout_percentage': (total_votes / total_voters * 100) if total_voters > 0 else 0
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)