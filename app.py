from flask import Flask, request, jsonify, abort
from ytmusicapi import YTMusic
import re
import requests
import logging
import os


app = Flask(__name__)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ytmusic = YTMusic("browser.json")

# Helper function to format Genius URL
def format_genius_url(artist, song_title):
    artist = re.sub(r"[^\w\s-]", "", artist).strip().replace(" ", "-")
    song_title = re.sub(r"[^\w\s-]", "", song_title).strip().replace(" ", "-")
    return f"https://genius.com/{artist}-{song_title}-lyrics"

# Helper function to scrape lyrics using regex
def scrape_lyrics(lyrics_url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        logger.info(f"Fetching lyrics from: {lyrics_url}")
        response = requests.get(lyrics_url, headers=headers, timeout=10)
        logger.info(f"Response status code: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"Failed to fetch lyrics: HTTP {response.status_code}")
            return None

        lyrics_pattern = re.compile(r'"lyrics":"(.*?)"', re.DOTALL)
        match = lyrics_pattern.search(response.text)

        if not match:
            logger.warning("No lyrics found in the page")
            return None

        lyrics = match.group(1).replace("\\n", "\n").replace("\\", "")
        return lyrics
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return None

# Search endpoint
@app.route("/search", methods=["GET"])
def search():
    query = request.args.get("query")
    filter = request.args.get("filter")
    if not query or not isinstance(query, str):
        abort(400, description="Query parameter is required and must be a string")

    try:
        results = ytmusic.search(query,filter)
        return jsonify(results)
    except Exception as e:
        logger.error(f"Error searching YTMusic: {e}")
        abort(500, description="An error occurred while processing your request")

# Song details endpoint
@app.route("/song/<song_id>", methods=["GET"])
def get_song(song_id):
    if not song_id or not isinstance(song_id, str):
        abort(400, description="Song ID is required and must be a string")

    try:
        details = ytmusic.get_song(song_id)
        return jsonify(details)
    except Exception as e:
        logger.error(f"Error fetching song details: {e}")
        abort(500, description="An error occurred while processing your request")

# Lyrics endpoint
@app.route("/lyrics", methods=["GET"])
def get_lyrics():
    song_title = request.args.get("title")
    artist_name = request.args.get("artist")

    if not song_title or not artist_name or not isinstance(song_title, str) or not isinstance(artist_name, str):
        abort(400, description="Title and artist parameters are required and must be strings")

    lyrics_url = format_genius_url(artist_name, song_title)
    lyrics = scrape_lyrics(lyrics_url)

    if not lyrics:
        fallback_url = f"https://api.lyrics.ovh/v1/{artist_name}/{song_title}"
        try:
            response = requests.get(fallback_url, timeout=10)
            if response.status_code == 200:
                lyrics = response.json().get("lyrics")
                logger.info(f"Fallback lyrics found for {song_title} by {artist_name}")
        except Exception as e:
            logger.error(f"Fallback error: {e}")

    if not lyrics:
        logger.warning(f"Lyrics not found for {song_title} by {artist_name}")
        abort(404, description="Lyrics not found on Genius or fallback service")

    return jsonify({"song_title": song_title, "artist": artist_name, "lyrics": lyrics})

# Related songs endpoint
@app.route("/related/<song_id>", methods=["GET"])
def get_related_songs(song_id):
    if not song_id or not isinstance(song_id, str):
        abort(400, description="Song ID is required and must be a string")

    try:
        watch_playlist = ytmusic.get_watch_playlist(song_id)
        related_songs = watch_playlist.get("tracks", [])
        return jsonify(related_songs)
    except Exception as e:
        logger.error(f"Error fetching related songs: {e}")
        abort(500, description="An error occurred while processing your request")
        # Artist details endpoint
@app.route("/artist/<artist_id>", methods=["GET"])
def get_artist_details(artist_id):
    if not artist_id or not isinstance(artist_id, str):
        abort(400, description="Artist ID is required and must be a string")
    
    try:
        
        details = ytmusic.get_artist(artist_id)
        return jsonify(details)
    except Exception as e:
        logger.error(f"Error fetching artist details: {e}")
        abort(500, description="An error occurred while processing your request")

# album endpoint
@app.route("/album/<album_id>", methods=["GET"])
def get_album_details(album_id):
    if not album_id or not isinstance(album_id, str):
        abort(400, description="Album ID is required and must be a string")

    try:
        details = ytmusic.get_album(album_id)
        return jsonify(details)
    except Exception as e:
        logger.error(f"Error fetching album details: {e}")
        abort(500, description="An error occurred while processing your request")
# Health check endpoint
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": str(error)}), 404

@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": str(error)}), 400

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": str(error)}), 500

# Run the app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)