from flask import Flask, request, jsonify, abort
from ytmusicapi import YTMusic
import re
import requests
from bs4 import BeautifulSoup
import logging
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os

# Initialize Flask app
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure caching
cache = Cache(app, config={"CACHE_TYPE": "simple"})

# Configure rate limiting
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])

# Initialize YTMusic API
oauth_file = os.getenv("OAUTH_FILE", "oauth.json")  # Use environment variable for oauth file
ytmusic = YTMusic(oauth_file)

# Helper function to format Genius URL
def format_genius_url(artist, song_title):
    artist = re.sub(r"[^\w\s]", "", artist)  # Remove special characters
    song_title = re.sub(r"[^\w\s]", "", song_title)  # Remove special characters
    artist = artist.replace(" ", "-").lower()
    song_title = song_title.replace(" ", "-").lower()
    return f"https://genius.com/{artist}-{song_title}-lyrics"

# Helper function to scrape lyrics from Genius
def scrape_lyrics(lyrics_url):
    try:
        response = requests.get(lyrics_url)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        lyrics_divs = soup.find_all("div", attrs={"data-lyrics-container": "true"})
        lyrics = "\n".join([div.get_text(separator="\n") for div in lyrics_divs])
        return lyrics
    except Exception as e:
        logger.error(f"Error scraping lyrics: {e}")
        return None

# Search endpoint
@app.route("/search", methods=["GET"])
@limiter.limit("10 per minute")  # Rate limit
@cache.cached(timeout=60)  # Cache results for 60 seconds
def search():
    query = request.args.get("query")
    if not query or not isinstance(query, str):
        abort(400, description="Query parameter is required and must be a string")

    try:
        results = ytmusic.search(query)
        return jsonify(results)
    except Exception as e:
        logger.error(f"Error searching YTMusic: {e}")
        abort(500, description="An error occurred while processing your request")

# Song details endpoint
@app.route("/song/<song_id>", methods=["GET"])
@limiter.limit("10 per minute")  # Rate limit
@cache.cached(timeout=60)  # Cache results for 60 seconds
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
@limiter.limit("10 per minute")  # Rate limit
@cache.cached(timeout=60)  # Cache results for 60 seconds
def get_lyrics():
    song_title = request.args.get("title")
    artist_name = request.args.get("artist")

    if not song_title or not artist_name or not isinstance(song_title, str) or not isinstance(artist_name, str):
        abort(400, description="Title and artist parameters are required and must be strings")

    try:
        lyrics_url = format_genius_url(artist_name, song_title)
        lyrics = scrape_lyrics(lyrics_url)

        if not lyrics:
            logger.warning(f"Lyrics not found for {song_title} by {artist_name}")
            abort(404, description="Lyrics not found")

        return jsonify({"song_title": song_title, "artist": artist_name, "lyrics": lyrics})
    except Exception as e:
        logger.error(f"Error fetching lyrics: {e}")
        abort(500, description="An error occurred while processing your request")

# Health check endpoint
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200

# Error handler for 404
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": str(error)}), 404

# Error handler for 400
@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": str(error)}), 400

# Error handler for 500
@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": str(error)}), 500

# Run the app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)