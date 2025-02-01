from flask import Flask, request, jsonify
from ytmusicapi import YTMusic
import os
import json

app = Flask(__name__)
oauth_credentials = os.getenv("O_Auth")
if not oauth_credentials:
    raise ValueError("OAuth credentials not found in environment variables.")

# Parse the credentials
credentials = json.loads(oauth_credentials)

# Initialize the API
ytmusic = YTMusic(auth=credentials)

@app.route("/search", methods=["GET"])
def search():
    query = request.args.get("query")
    if not query:
        return jsonify({"error": "Query parameter is required"}), 400

    results = ytmusic.search(query)
    return jsonify(results)

@app.route("/song/<song_id>", methods=["GET"])
def get_song(song_id):
    details = ytmusic.get_song(song_id)
    return jsonify(details)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)