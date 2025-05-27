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
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
from waitress import serve
from datetime import datetime,timedelta
from redis import from_url as redis_from_url




app = Flask(__name__)
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

app.config.update({
    'CACHE_TYPE': 'RedisCache',
    'CACHE_REDIS_URL': redis_url,
    'CACHE_DEFAULT_TIMEOUT': 86400,
    'CACHE_THRESHOLD': 5000,
    'CACHE_KEY_PREFIX': 'ytmusic_api_'
})
cache = Cache(app)
original_get = requests.get

def make_daily_cache_key():
    """Generate a cache key that includes the current date"""
    current_date = datetime.now().strftime("%Y-%m-%d")
    artist_ids_param = request.args.get("artists", "")
    limit_param = request.args.get("limit", "50")
    return f"mix_daily_{current_date}_{artist_ids_param}_{limit_param}"

def make_recommendations_cache_key(song_ids):
    """Generate a cache key for recommendations that includes the current date and song_ids"""
    current_date = datetime.now().strftime("%Y-%m-%d")
    
    # Sort song_ids to ensure consistent cache keys regardless of order
    sorted_song_ids = sorted(song_ids)
    song_ids_str = ','.join(sorted_song_ids)
    
    return f"recommendations_daily_{current_date}_{hash(song_ids_str)}"

def get_seconds_until_midnight():
    """Calculate seconds remaining until midnight for true daily refresh"""
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds())
def make_trending_cache_key():
    """Generate a cache key for trending that includes the current date and parameters"""
    current_date = datetime.now().strftime("%Y-%m-%d")
    country_param = request.args.get("country", "US")
    limit_param = request.args.get("limit", "50")
    
    return f"trending_daily_{current_date}_{country_param}_{limit_param}"

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
    # Generate cache key
    cache_key = make_trending_cache_key()
    
    # Check cache first
    cached_data = cache.get(cache_key)
    if cached_data:
        logger.info(f"Returning cached trending data for key: {cache_key}")
        return jsonify(cached_data)
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
         # Cache until midnight for true daily refresh
        cache.set(cache_key, trending_songs, timeout=get_seconds_until_midnight())
        logger.info(f"Cached trending data for key: {cache_key}")
        return jsonify(trending_songs)
    except Exception as e:
        logger.error(f"Error fetching trending songs: {e}")
        abort(500, description="An error occurred while fetching trending songs")

# Billboard songs endpoint
async def async_fetch_billboard_song(entry):
    """Fetch additional song details for a Billboard chart entry"""
    try:
        query = f"{entry.title} {entry.artist}"
        # Use asyncio.to_thread to run blocking operations asynchronously
        results = await asyncio.to_thread(ytmusic.search, query, filter="songs")
        best_match = results[0] if results else {}
        
        # Parse and fetch artist data
        artists = await parse_and_fetch_artists(entry.artist)
        
        return {
            "rank": entry.rank,
            "title": entry.title,
            "artists": artists,  # Changed from "artist" to "artists"
            "lastPos": entry.lastPos,
            "peakPos": entry.peakPos,
            "weeks": entry.weeks,
            "ytmusic_result": best_match  
        }
    except Exception as e:
        logger.error(f"Error fetching details for {entry.title}: {e}")
        return {
            "rank": entry.rank,
            "title": entry.title,
            "artists": [{"id": None, "name": entry.artist}],  # Fallback format
            "lastPos": entry.lastPos,
            "peakPos": entry.peakPos,
            "weeks": entry.weeks,
            "ytmusic_result": {}
        }
async def parse_and_fetch_artists(artist_string):
    """Parse artist string and fetch individual artist IDs from YouTube Music"""
    # Common separators used in Billboard for collaborations
    separators = [' & ', ' and ', ' feat. ', ' featuring ', ' ft. ', ' with ', ', ']
    
    # Split the artist string by common separators
    artists = [artist_string]
    for separator in separators:
        new_artists = []
        for artist in artists:
            new_artists.extend([a.strip() for a in artist.split(separator)])
        artists = new_artists
    
    # Remove empty strings and duplicates while preserving order
    artists = list(dict.fromkeys([a for a in artists if a.strip()]))
    
    result_artists = []
    
    # Fetch artist ID for each artist
    for artist_name in artists:
        try:
            # Search for the artist on YouTube Music
            search_results = await asyncio.to_thread(
                ytmusic.search, 
                artist_name, 
                filter="artists"
            )
            
            if search_results:
                # Get the first result which should be the most relevant
                artist_data = search_results[0]
                artist_id = artist_data.get('browseId') or artist_data.get('id')
                result_artists.append({
                    "id": artist_id,
                    "name": artist_name
                })
            else:
                # If no results found, add with null ID
                result_artists.append({
                    "id": None,
                    "name": artist_name
                })
        except Exception as e:
            logger.error(f"Error fetching artist ID for {artist_name}: {e}")
            # Add with null ID if search fails
            result_artists.append({
                "id": None,
                "name": artist_name
            })
    
    return result_artists


def get_billboard_chart(chart_name='hot-100', date=None):
    """Get Billboard chart data with error handling"""
    try:
        return billboard.ChartData(chart_name, date=date)
    except Exception as e:
        logger.error(f"Error fetching Billboard chart {chart_name}: {e}")
        return None

def register_billboard_routes(app):
    """Register all Billboard-related routes"""
def get_billboard_cache_key():
    """Generate cache key based on current week to auto-refresh when Billboard updates"""
   
    
    # Billboard typically updates on Tuesdays
    today = datetime.now()
    # Calculate the Tuesday of current week
    days_since_tuesday = (today.weekday() - 1) % 7
    current_tuesday = today - timedelta(days=days_since_tuesday)
    week_key = current_tuesday.strftime("%Y-%m-%d")
    
    return f"billboard_hot_100_{week_key}"

def get_seconds_until_next_tuesday():
    """Calculate seconds until next Tuesday (Billboard update day)"""
    now = datetime.now()
    days_ahead = 1 - now.weekday()  # Tuesday is 1
    if days_ahead <= 0:  # Target day already happened this week
        days_ahead += 7
    next_tuesday = now + timedelta(days=days_ahead)
    next_tuesday = next_tuesday.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((next_tuesday - now).total_seconds())

    
    
@app.route("/billboard", methods=["GET"])
async def billboard_songs():
    cache_key = get_billboard_cache_key()  # Use date-based cache key
    cached_data = cache.get(cache_key)
    
    if cached_data:
        logger.info(f"Returning cached Billboard data for key: {cache_key}")
        return jsonify(cached_data)
    
    try:
        chart = await asyncio.to_thread(billboard.ChartData, 'hot-100')
        
        # Fetch ALL 100 songs at once
        songs = []
        for entry in chart:  # Remove slicing, get all entries
            song = await async_fetch_billboard_song(entry)
            songs.append(song)
        
        response_data = {
            "data": songs,
            "metadata": {
                "total_items": len(chart),
                "chart_date": chart.date,  # Include chart date for reference
                "last_updated": datetime.now().isoformat()
            }
        }
        
        cache.set(cache_key, response_data, timeout=get_seconds_until_next_tuesday())
        logger.info(f"Cached Billboard data for key: {cache_key}")
        
        return jsonify(response_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500



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

# Mix Endpoint
def format_thumbnails(thumbnails):
    """
    Format a list of thumbnails to include only height, url, and width.
    """
    if not thumbnails:
        return []
    formatted = []
    for thumb in thumbnails:
        formatted.append({
            "height": thumb.get("height"),
            "width": thumb.get("width"),
            "url": thumb.get("url")
        })
    return formatted

@app.route("/mix", methods=["GET"])
def mix_songs():
    import random
    from collections import defaultdict
    
    # Generate cache key
    cache_key = make_daily_cache_key()
    
    # Check cache first
    cached_data = cache.get(cache_key)
    if cached_data:
        logger.info(f"Returning cached mix data for key: {cache_key}")
        return jsonify(cached_data)
    
    artist_ids_param = request.args.get("artists")
    limit_param = request.args.get("limit", "50")
    
    if not artist_ids_param:
        abort(400, description="artists parameter is required (comma-separated artist IDs)")
    
    # Parse limit parameter
    try:
        limit = int(limit_param)
        if limit <= 0:
            limit = 50
    except ValueError:
        limit = 50
    
    artist_ids = [x.strip() for x in artist_ids_param.split(",")]
    all_songs_by_artist = {}  # Store songs grouped by artist
    
    # Collect songs for each artist separately
    for artist_id in artist_ids:
        artist_songs = []
        try:
            artist_info = ytmusic.get_artist(artist_id)
        except Exception as e:
            logger.error(f"Error retrieving artist {artist_id}: {e}")
            continue
        
        # Handle 'songs' key: these are the artist's top songs/videos
        if "songs" in artist_info:
            songs_data = artist_info["songs"]
            songs_browse_id = songs_data.get("browseId")
            if songs_browse_id:
                try:
                    playlist_data = ytmusic.get_playlist(songs_browse_id)
                    for track in playlist_data.get("tracks", []):
                        raw_thumbnails = track.get("thumbnails")
                        if track.get("artists"):
                            artists = [
                                {"id": a.get("id") or a.get("channelId"), "name": a.get("name")}
                                for a in track.get("artists")
                            ]
                        elif track.get("artist"):
                            artists = [{"id": None, "name": track.get("artist")}]
                        else:
                            artists = []
                        
                        artist_songs.append({
                            "title": track.get("title"),
                            "videoId": track.get("videoId"),
                            "artists": artists,
                            "album": None,
                            "duration": track.get("duration"),
                            "thumbnails": format_thumbnails(raw_thumbnails)
                        })
                except Exception as e:
                    logger.error(f"Error fetching playlist for songs from artist {artist_id}: {e}")
            else:
                for song in songs_data.get("results", []):
                    raw_thumbnails = song.get("thumbnails")
                    if song.get("artists"):
                        artists = [
                            {"id": a.get("id") or a.get("channelId"), "name": a.get("name")}
                            for a in song.get("artists")
                        ]
                    elif song.get("artist"):
                        artists = [{"id": None, "name": song.get("artist")}]
                    else:
                        artists = []
                    
                    artist_songs.append({
                        "title": song.get("title"),
                        "videoId": song.get("videoId"),
                        "artists": artists,
                        "album": song.get("album"),
                        "duration": song.get("duration"),
                        "thumbnails": format_thumbnails(raw_thumbnails)
                    })
        
        # Handle albums and singles
        for content_type in ["albums", "singles"]:
            if content_type in artist_info:
                content_data = artist_info[content_type]
                params = content_data.get("params")
                if params:
                    try:
                        releases = ytmusic.get_artist_albums(artist_id, params)
                        for release in releases:
                            album_id = release.get("browseId")
                            if album_id:
                                try:
                                    album_info = ytmusic.get_album(album_id)
                                    album_thumbnails = album_info.get("thumbnails", [])
                                    for track in album_info.get("tracks", []):
                                        raw_thumbnails = track.get("thumbnails") or album_thumbnails
                                        if track.get("artists"):
                                            artists = [
                                                {"id": a.get("id") or a.get("channelId"), "name": a.get("name")}
                                                for a in track.get("artists")
                                            ]
                                        elif album_info.get("artists"):
                                            artists = [
                                                {"id": a.get("id") or a.get("channelId"), "name": a.get("name")}
                                                for a in album_info.get("artists")
                                            ]
                                        else:
                                            artists = []
                                        
                                        artist_songs.append({
                                            "title": track.get("title"),
                                            "videoId": track.get("videoId"),
                                            "artists": artists,
                                            "album": album_info.get("title"),
                                            "duration": track.get("duration"),
                                            "thumbnails": format_thumbnails(raw_thumbnails)
                                        })
                                except Exception as e:
                                    logger.error(f"Error fetching album {album_id}: {e}")
                    except Exception as e:
                        logger.error(f"Error fetching {content_type} for artist {artist_id}: {e}")
        
        # Remove duplicates for this artist
        seen = set()
        unique_artist_songs = []
        for song in artist_songs:
            identifier = song.get("videoId") or song.get("title")
            if identifier and identifier not in seen:
                seen.add(identifier)
                unique_artist_songs.append(song)
        
        # Shuffle this artist's songs and store them
        random.shuffle(unique_artist_songs)
        all_songs_by_artist[artist_id] = unique_artist_songs

    # **BEST METHOD: Balanced Round-Robin Selection**
    def create_balanced_mix(songs_by_artist, total_limit):
        """
        Creates a balanced mix by distributing songs evenly across artists
        using a round-robin approach.
        """
        if not songs_by_artist:
            return []
        
        # Calculate songs per artist for balanced distribution
        num_artists = len(songs_by_artist)
        base_songs_per_artist = total_limit // num_artists
        extra_songs = total_limit % num_artists
        
        result = []
        artist_indices = {artist_id: 0 for artist_id in songs_by_artist.keys()}
        artists_list = list(songs_by_artist.keys())
        
        # First pass: Give each artist their base allocation
        for round_num in range(base_songs_per_artist):
            for artist_id in artists_list:
                songs = songs_by_artist[artist_id]
                if artist_indices[artist_id] < len(songs):
                    result.append(songs[artist_indices[artist_id]])
                    artist_indices[artist_id] += 1
        
        # Second pass: Distribute remaining songs
        artist_idx = 0
        for _ in range(extra_songs):
            while artist_idx < len(artists_list):
                artist_id = artists_list[artist_idx]
                songs = songs_by_artist[artist_id]
                if artist_indices[artist_id] < len(songs):
                    result.append(songs[artist_indices[artist_id]])
                    artist_indices[artist_id] += 1
                    artist_idx += 1
                    break
                artist_idx += 1
        
        # Final shuffle to mix the order while maintaining balance
        random.shuffle(result)
        return result[:total_limit]

    # Create balanced mix
    result = create_balanced_mix(all_songs_by_artist, limit)
    
    # Log artist distribution for debugging
    artist_count = defaultdict(int)
    for song in result:
        for artist in song.get('artists', []):
            artist_name = artist.get('name', 'Unknown')
            artist_count[artist_name] += 1
    
    logger.info(f"Artist distribution in mix: {dict(artist_count)}")
    
    # Cache the result
    cache.set(cache_key, result, timeout=get_seconds_until_midnight())
    logger.info(f"Cached mix data for key: {cache_key}")
    
    return jsonify(result)

def transform_song_data(song_data):
    """
    Transform the song data retrieved from ytmusicapi to match the desired response structure.
    """
    try:
        video_details = song_data.get('videoDetails', {})
        microformat = song_data.get('microformat', {}).get('microformatDataRenderer', {})
        streaming_data = song_data.get('streamingData', {})
        player_overlays = song_data.get('playerOverlays', {}).get('playerOverlayRenderer', {})
        music_analytics = song_data.get('musicAnalytics', {})

        # Extract artist information
        artists = video_details.get('author', '')
        artist_name = artists if isinstance(artists, str) else ', '.join(artists)
        artist_id = video_details.get('channelId', '')

        # Extract thumbnails
        thumbnails = video_details.get('thumbnail', {}).get('thumbnails', [])
        formatted_thumbnails = [{'url': thumb.get('url'), 'width': thumb.get('width'), 'height': thumb.get('height')} for thumb in thumbnails]

        # Extract feedback tokens
        feedback_tokens = music_analytics.get('feedbackTokens', {})
        add_token = feedback_tokens.get('add', '')
        remove_token = feedback_tokens.get('remove', '')

        # Construct the transformed data
        transformed_data = {
            "album": None,  # Album information might not be available
            "artists": [
                {
                    "id": artist_id,
                    "name": artist_name
                }
            ],
            "category": microformat.get('category', None),
            "duration": video_details.get('lengthSeconds', '0'),
            "duration_seconds": int(video_details.get('lengthSeconds', '0')),
            "feedbackTokens": {
                "add": add_token,
                "remove": remove_token
            },
            "inLibrary": False,  # This information might not be directly available
            "isExplicit": video_details.get('isLive', False),
            "resultType": "song",
            "thumbnails": formatted_thumbnails,
            "title": video_details.get('title', ''),
            "videoId": video_details.get('videoId', ''),
            "videoType": video_details.get('isLive', False) and "LIVE" or "MUSIC_VIDEO_TYPE_ATV",
            "year": None  # Year information might not be available
        }
        return transformed_data
    except Exception as e:
        logger.error(f"Error transforming song data: {e}")
        return None

@app.route("/songs", methods=["POST"])
def get_songs():
    song_ids = request.json.get("song_ids", [])
    
    if not song_ids or not isinstance(song_ids, list):
        abort(400, description="A list of song IDs is required.")
    
    songs_data = []
    for song_id in song_ids:
        try:
            song_data = ytmusic.get_song(song_id)
            transformed_data = transform_song_data(song_data)
            if transformed_data:
                songs_data.append(transformed_data)
            else:
                songs_data.append({"error": f"Failed to transform data for song ID {song_id}"})
        except Exception as e:
            logger.error(f"Error fetching data for song ID {song_id}: {e}")
            songs_data.append({"error": f"Failed to fetch data for song ID {song_id}"})
    

    return jsonify(songs_data)

#recommendation endpoint
@app.route("/recommendations", methods=["POST"])
def get_recommendations():
    data = request.get_json()
    if not data or 'song_ids' not in data:
        abort(400, description="Missing 'song_ids' in request body")
    
    song_ids = data['song_ids']
    if not isinstance(song_ids, list) or len(song_ids) < 1 or len(song_ids) > 50:
        abort(400, description="song_ids must be a list containing 1 to 50 song IDs")
    cache_key = make_recommendations_cache_key(song_ids)  
    
    # Check cache first
    cached_data = cache.get(cache_key)
    if cached_data:
        logger.info(f"Returning cached recommendations for key: {cache_key}")
        return jsonify(cached_data)
    
    # If not in cache, generate recommendations
    all_tracks = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        # Create a future for each song ID
        futures = {executor.submit(ytmusic.get_watch_playlist, song_id): song_id for song_id in song_ids}
        
        for future in as_completed(futures):
            song_id = futures[future]
            try:
                result = future.result()
                tracks = result.get('tracks', [])
                # Enrich track data with seed song ID for debugging
                for track in tracks:
                    track['seedSongId'] = song_id
                all_tracks.extend(tracks)
            except Exception as e:
                logger.error(f"Error processing song {song_id}: {e}")
    
    # Deduplicate tracks while preserving order
    seen = set()
    unique_tracks = []
    for track in all_tracks:
        video_id = track.get('videoId')
        if video_id and video_id not in seen:
            seen.add(video_id)
            # Remove temporary seedSongId before returning
            track.pop('seedSongId', None)
            unique_tracks.append(track)
    
    # Prioritize tracks that appear in multiple seed song recommendations
    track_counts = {}
    for track in all_tracks:
        video_id = track.get('videoId')
        if video_id:
            track_counts[video_id] = track_counts.get(video_id, 0) + 1
    
    # Add randomness while still considering relevance
    # Group tracks by frequency
    frequency_groups = {}
    for track in unique_tracks:
        count = track_counts[track['videoId']]
        if count not in frequency_groups:
            frequency_groups[count] = []
        frequency_groups[count].append(track)
    
    # Shuffle each frequency group
    for count in frequency_groups:
        random.shuffle(frequency_groups[count])
    
    # Rebuild the list with some randomness
    result_tracks = []
    counts = sorted(frequency_groups.keys(), reverse=True)
    
    # Ensure we get tracks from all frequency groups
    while len(result_tracks) < 50 and frequency_groups:
        # Randomly select a frequency group with weighted probability
        weights = [c for c in counts if frequency_groups[c]]
        if not weights:
            break
            
        selected_count = random.choices(
            weights,
            weights=weights,  # Weight by frequency
            k=1
        )[0]
        
        # Take a track from this group
        if frequency_groups[selected_count]:
            track = frequency_groups[selected_count].pop(0)
            result_tracks.append(track)
            
            # Remove empty groups
            if not frequency_groups[selected_count]:
                frequency_groups.pop(selected_count)
                counts.remove(selected_count)
    
    # If we need more tracks, take from unique_tracks
    if len(result_tracks) < 50:
        # Get tracks that weren't already selected
        remaining_ids = set(t['videoId'] for t in unique_tracks) - set(t['videoId'] for t in result_tracks)
        remaining_tracks = [t for t in unique_tracks if t['videoId'] in remaining_ids]
        random.shuffle(remaining_tracks)
        result_tracks.extend(remaining_tracks[:50-len(result_tracks)])
    
    final_result = result_tracks[:50]
    
    # Cache until midnight for true daily refresh
    cache.set(cache_key, final_result, timeout=get_seconds_until_midnight())
    logger.info(f"Cached recommendations for key: {cache_key}")
    
    return jsonify(final_result)


# search Suggestion endpoint
@app.route("/search_suggestions", methods=["GET"])
def get_search_suggestions():
    query = request.args.get("query")
    
    if not query or not isinstance(query, str):
        abort(400, description="Query parameter is required and must be a string")

    try:
        suggestions = ytmusic.get_search_suggestions(query)
        return jsonify(suggestions)
    except Exception as e:
        logger.error(f"Error fetching search suggestions: {e}")
        abort(500, description="An error occurred while processing your request")
# Multiple artists endpoint
@app.route("/artists", methods=["POST"])
def get_multiple_artists():
    """
    Fetch multiple artists' basic information including high quality thumbnail, name, and browseId
    Expected input: {"artist_ids": ["browseId1", "browseId2", ...]}
    """
    data = request.get_json()
    
    if not data or 'artist_ids' not in data:
        abort(400, description="Missing 'artist_ids' in request body")
    
    artist_ids = data['artist_ids']
    
    if not isinstance(artist_ids, list):
        abort(400, description="artist_ids must be a list")
    
    if len(artist_ids) == 0:
        abort(400, description="artist_ids list cannot be empty")
    
    if len(artist_ids) > 50:  # Reasonable limit to prevent abuse
        abort(400, description="Maximum 50 artist IDs allowed per request")
    
    # Validate that all items in the list are strings
    for artist_id in artist_ids:
        if not isinstance(artist_id, str) or not artist_id.strip():
            abort(400, description="All artist IDs must be non-empty strings")
    
    def fetch_artist_info(artist_id):
        """Helper function to fetch individual artist info"""
        try:
            artist_details = ytmusic.get_artist(artist_id)
            
            # Get the highest quality thumbnail
            thumbnails = artist_details.get('thumbnails', [])
            high_quality_thumbnail = None
            
            if thumbnails:
                # Sort by resolution (width * height) to get the highest quality
                sorted_thumbnails = sorted(
                    thumbnails, 
                    key=lambda x: (x.get('width', 0) * x.get('height', 0)), 
                    reverse=True
                )
                high_quality_thumbnail = {
                    "url": sorted_thumbnails[0].get('url'),
                    "width": sorted_thumbnails[0].get('width'),
                    "height": sorted_thumbnails[0].get('height')
                }
            
            return {
                "browseId": artist_id,
                "name": artist_details.get('name'),
                "thumbnail": high_quality_thumbnail,
                "success": True
            }
            
        except Exception as e:
            logger.error(f"Error fetching artist {artist_id}: {e}")
            return {
                "browseId": artist_id,
                "name": None,
                "thumbnail": None,
                "success": False,
                "error": str(e)
            }
    
    # Use ThreadPoolExecutor for concurrent requests
    artists_data = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        # Submit all tasks
        future_to_artist = {
            executor.submit(fetch_artist_info, artist_id): artist_id 
            for artist_id in artist_ids
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_artist):
            artist_result = future.result()
            artists_data.append(artist_result)
    
    # Sort results to match the input order
    id_to_result = {result['browseId']: result for result in artists_data}
    ordered_results = [id_to_result[artist_id] for artist_id in artist_ids]
    
    # Separate successful and failed requests for better response structure
    successful_artists = [artist for artist in ordered_results if artist['success']]
    failed_artists = [artist for artist in ordered_results if not artist['success']]
    
    # Clean up successful results (remove success flag and error field)
    for artist in successful_artists:
        artist.pop('success', None)
        artist.pop('error', None)
    
    response_data = {
        "artists": successful_artists,
        "total_requested": len(artist_ids),
        "total_successful": len(successful_artists),
        "total_failed": len(failed_artists)
    }
    
    # Include failed requests info if any
    if failed_artists:
        response_data["failed_requests"] = [
            {
                "browseId": artist['browseId'],
                "error": artist['error']
            } for artist in failed_artists
        ]
    
    return jsonify(response_data)
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
    serve(app, host="0.0.0.0", port=5000)