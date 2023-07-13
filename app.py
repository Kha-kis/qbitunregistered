from flask import Flask, render_template, jsonify
from qbitunregistered import client

app = Flask(__name__)

# Routes
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/torrents')
def torrents():
    torrents = client.torrents.info()
    return render_template('torrents.html', torrents=torrents)

@app.route('/pause_torrents')
def pause_torrents():
    client.torrents_pause_all()
    return jsonify({'message': 'Torrents paused successfully.'})

@app.route('/resume_torrents')
def resume_torrents():
    client.torrents_resume_all()
    return jsonify({'message': 'Torrents resumed successfully.'})

if __name__ == '__main__':
    app.run(debug=True)
