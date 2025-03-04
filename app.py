from flask import Flask, request, jsonify, abort
from ytmusicapi import YTMusic
from flask_caching import Cache
import billboard
import asyncio 
import re
import requests
import logging
import concurrent.futures
import os


app = Flask(__name__)
original_get = requests.get

def patched_get(url, *args, **kwargs):
    headers = kwargs.get("headers", {})
    # If there's no User-Agent, add one that mimics a browser.
    if "User-Agent" not in headers:
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    kwargs["headers"] = headers
    return original_get(url, *args, **kwargs)

# Patch requests.get globally
requests.get = patched_get

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
        results = ytmusic.search(query,filter,)
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
@app.route("/artist/<artist_id>/songs", methods=["GET"])
def get_artist_songs(artist_id):
    if not artist_id or not isinstance(artist_id, str):
        abort(400, description="Artist ID is required and must be a string")
    
    try:
        artist_details = ytmusic.get_artist(artist_id)
        all_songs = []
        
        # Process different release sections (albums, singles, etc.)
        for section in artist_details.get('sections', []):
            section_title = section.get('title', '').lower()
            
            # Handle different types of releases
            if any(keyword in section_title for keyword in ['album', 'single', 'ep', 'compilation']):
                for item in section.get('items', []):
                    album_id = item.get('browseId')
                    if album_id:
                        try:
                            album_details = ytmusic.get_album(album_id)
                            # Extract relevant track information
                            for track in album_details.get('tracks', []):
                                simplified_track = {
                                    "title": track.get('title'),
                                    "videoId": track.get('videoId'),
                                    "artist": track.get('artists')[0].get('name') if track.get('artists') else None,
                                    "album": album_details.get('title'),
                                    "duration": track.get('duration'),
                                    "year": album_details.get('year')
                                }
                                all_songs.append(simplified_track)
                        except Exception as e:
                            logger.error(f"Error processing album {album_id}: {e}")
                            continue

        # Remove duplicates while preserving order
        seen = set()
        unique_songs = []
        for song in all_songs:
            identifier = song.get('videoId') or song.get('title')
            if identifier and identifier not in seen:
                seen.add(identifier)
                unique_songs.append(song)

        return jsonify(unique_songs)

    except Exception as e:
        logger.error(f"Error fetching artist songs: {e}")
        abort(500, description="An error occurred while processing your request")
# Trending songs endpoint


def fetch_song(video):
    title = video.get("title", "")
    artists = video.get("artists", [])
    artist_name = artists[0].get("name") if artists else ""
    query = f"{title} {artist_name}".strip()
    search_results = ytmusic.search(query, filter="songs")
    return search_results[0] if search_results else video

@app.route("/trending", methods=["GET"])
def trending_songs():
    country = request.args.get("country", "US")
    limit_param = request.args.get("limit", "50")
    try:
        limit = int(limit_param)
    except ValueError:
        limit = 50

    try:
        charts = ytmusic.get_charts(country=country)
        trending_video_items = charts.get("videos", {}).get("items", [])[:limit]
        
        # Use a thread pool to search for songs concurrently.
        with concurrent.futures.ThreadPoolExecutor() as executor:
            trending_songs = list(executor.map(fetch_song, trending_video_items))
        
        return jsonify(trending_songs)
    except Exception as e:
        logger.error(f"Error fetching trending songs: {e}")
        abort(500, description="An error occurred while fetching trending songs")

# Billboard songs endpoint

async def async_fetch_billboard_song(entry):
    query = f"{entry.title} {entry.artist}"
    # Wrap the blocking ytmusic.search call so it can run concurrently.
    results = await asyncio.to_thread(ytmusic.search, query, filter="songs")
    best_match = results[0] if results else {}
    return {
        "rank": entry.rank,
        "title": entry.title,
        "artist": entry.artist,
        "lastPos": entry.lastPos,
        "peakPos": entry.peakPos,
        "weeks": entry.weeks,
        "ytmusic_result": best_match  
    }
# Configure caching (simple in-memory cache for demonstration)
cache = Cache(app, config={'CACHE_TYPE': 'simple'})
@app.route("/billboard", methods=["GET"])
async def billboard_songs():
    # Get pagination parameters with defaults (e.g., 10 songs per page)
    limit_param = request.args.get("limit", "10")
    offset_param = request.args.get("offset", "0")
    try:
        limit = int(limit_param)
        offset = int(offset_param)
    except ValueError:
        limit = 10
        offset = 0

    # Create a unique cache key based on the limit and offset.
    cache_key = f"billboard_{offset}_{limit}"
    cached_entries = cache.get(cache_key)
    if cached_entries:
        return jsonify(cached_entries)
    
    try:
        # Fetch the full Billboard chart data once.
        chart = await asyncio.to_thread(billboard.ChartData, 'hot-100')
        # Slice the chart based on pagination parameters.
        chart_subset = chart[offset:offset + limit]
        # Create asynchronous tasks for each song in the subset.
        tasks = [async_fetch_billboard_song(entry) for entry in chart_subset]
        entries = await asyncio.gather(*tasks)
        # Cache the result for 60 seconds.
        cache.set(cache_key, entries, timeout=1000)
        return jsonify(entries)
    except Exception as e:
        logger.error(f"Error fetching Billboard songs: {e}")
        abort(500, description="An error occurred while fetching Billboard songs")


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