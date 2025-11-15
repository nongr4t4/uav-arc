from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app)


@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({
        'status': 'ok',
        'time': datetime.utcnow().isoformat() + 'Z'
    })


@app.route('/echo', methods=['POST'])
def echo():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({'error': 'invalid json'}), 400
    return jsonify({'received': payload, 'time': datetime.utcnow().isoformat() + 'Z'})


if __name__ == '__main__':
    # Runs on port 8000 so it's easy to expose on many hosts
    app.run(host='0.0.0.0', port=8000)
