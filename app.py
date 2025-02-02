

from flask import Flask, request, jsonify, abort
from ytmusicapi import YTMusic
import re
import requests
import logging
import os
from bs4 import BeautifulSoup

# Initialize Flask app
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)



ytmusic = YTMusic("browser.json")


# Search endpoint
@app.route("/search", methods=["GET"])

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

def format_genius_url(artist, song_title):
    # Convert artist and song_title to Genius URL format
    artist = re.sub(r"[^\w\s]", "", artist)  # Remove special characters
    song_title = re.sub(r"[^\w\s]", "", song_title)  # Remove special characters
    artist = artist.replace(" ", "-").lower()
    song_title = song_title.replace(" ", "-").lower()
    return f"https://genius.com/{artist}-{song_title}-lyrics"
def scrape_lyrics(lyrics_url):
    try:
        response = requests.get(lyrics_url)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        # The lyrics are usually inside a <div> tag with data-lyrics-container
        lyrics_divs = soup.find_all("div", attrs={"data-lyrics-container": "true"})
        lyrics = "\n".join([div.get_text(separator="\n") for div in lyrics_divs])
        return lyrics
    except Exception as e:
        print(f"Error scraping lyrics: {e}")
        return None
@app.route("/lyrics", methods=["GET"])
def get_lyrics():
    song_title = request.args.get("title")
    artist_name = request.args.get("artist")
    if not song_title or not artist_name:
        return jsonify({"error": "Title and artist parameters are required"}), 400
    # Construct Genius URL
    lyrics_url = format_genius_url(artist_name, song_title)
    # Scrape lyrics
    lyrics = scrape_lyrics(lyrics_url)
    if not lyrics:
        return jsonify({"error": "Lyrics not found or unable to scrape"}), 404
    return jsonify({"song_title": song_title, "artist": artist_name, "lyrics": lyrics})

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



