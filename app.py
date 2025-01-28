from flask import Flask, request, jsonify
from ytmusicapi import YTMusic
import re
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
ytmusic = YTMusic("oauth.json")
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

if __name__ == "__main__":
    app.run()
